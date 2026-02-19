#!/bin/bash
set -euo pipefail

INPUT_DIR="/app/input"
OUTPUT_DIR="/app/output"
PROCESSING_DIR="/app/processing"
LOG_FILE="/app/logs/convert.log"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
# 限制 ffmpeg 使用的线程数，默认 2，防止耗尽宿主机 CPU
FFMPEG_THREADS="${FFMPEG_THREADS:-2}"
# CRF 质量参数，默认 20（适合 Web）
CRF="${CRF:-20}"
# 视频文件扩展名
VIDEO_EXTENSIONS="mp4 mkv avi mov wmv flv webm ts m4v mpg mpeg 3gp"

mkdir -p "$INPUT_DIR" "$OUTPUT_DIR" "$PROCESSING_DIR" "$(dirname "$LOG_FILE")"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg" | tee -a "$LOG_FILE"
}

is_video_file() {
    local ext="${1##*.}"
    ext=$(echo "$ext" | tr '[:upper:]' '[:lower:]')
    for v in $VIDEO_EXTENSIONS; do
        [ "$ext" = "$v" ] && return 0
    done
    return 1
}

# 检查文件是否写入完成（大小不再变化）
is_file_stable() {
    local file="$1"
    local size1 size2
    size1=$(stat -c%s "$file" 2>/dev/null || echo 0)
    sleep 2
    size2=$(stat -c%s "$file" 2>/dev/null || echo 0)
    [ "$size1" = "$size2" ] && [ "$size1" -gt 0 ]
}

convert_video() {
    local input_file="$1"
    local filename
    filename=$(basename "$input_file")
    local name_no_ext="${filename%.*}"
    local processing_file="$PROCESSING_DIR/$filename"
    local output_file="$OUTPUT_DIR/${name_no_ext}.mp4"

    # 移动到处理目录，避免重复拾取
    mv "$input_file" "$processing_file"
    log "开始转换: $filename"

    # 如果输出文件已存在，加时间戳
    if [ -f "$output_file" ]; then
        output_file="$OUTPUT_DIR/${name_no_ext}_$(date +%s).mp4"
    fi

    local tmp_output="$PROCESSING_DIR/${name_no_ext}_tmp.mp4"

    # 获取视频时长（秒），用于计算进度百分比
    local duration
    duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$processing_file" 2>/dev/null | cut -d. -f1)
    duration=${duration:-0}

    # 使用 nice 降低进程优先级，防止抢占宿主机资源
    # -loglevel warning: 仅输出警告和错误，不输出逐帧进度
    # -stats_period 60: 每 60 秒输出一次统计信息
    if nice -n 19 ffmpeg -y -loglevel warning -stats_period 60 -i "$processing_file" \
        -c:v libx264 \
        -preset slow \
        -crf "$CRF" \
        -profile:v high \
        -level 4.1 \
        -threads "$FFMPEG_THREADS" \
        -vf "scale='min(iw,1920)':'min(ih,1080)':force_original_aspect_ratio=decrease:flags=lanczos,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p" \
        -movflags +faststart \
        -c:a aac -b:a 128k -ac 2 \
        -f mp4 \
        "$tmp_output" \
        >> "$LOG_FILE" 2>&1; then

        mv "$tmp_output" "$output_file"
        rm -f "$processing_file"
        log "转换完成: $filename -> $(basename "$output_file")"
    else
        log "转换失败: $filename"
        # 失败文件移回 input 等待重试或人工处理
        mv "$processing_file" "$INPUT_DIR/${name_no_ext}.failed.${filename##*.}"
        rm -f "$tmp_output"
    fi
}

# 启动时恢复上次中断的文件（processing 目录残留）
recover_interrupted() {
    for file in "$PROCESSING_DIR"/*; do
        [ -f "$file" ] || continue
        local filename
        filename=$(basename "$file")
        # 跳过临时输出文件
        [[ "$filename" == *_tmp.mp4 ]] && { rm -f "$file"; continue; }
        log "恢复中断文件: $filename"
        mv "$file" "$INPUT_DIR/$filename"
    done
}

log "========================================="
log "FFmpeg 视频转换服务启动 v1.0.0"
log "线程数: $FFMPEG_THREADS | CRF: $CRF | 轮询间隔: ${POLL_INTERVAL}s"
log "========================================="

recover_interrupted

# 主循环：轮询监听目录
while true; do
    found=0
    for file in "$INPUT_DIR"/*; do
        [ -f "$file" ] || continue
        is_video_file "$file" || continue

        # 跳过之前转换失败的文件
        [[ "$(basename "$file")" == *.failed.* ]] && continue

        if is_file_stable "$file"; then
            convert_video "$file"
            found=1
            break  # 每次只处理一个，处理完重新扫描
        else
            log "文件写入中，跳过: $(basename "$file")"
        fi
    done

    # 没有文件时等待
    [ "$found" -eq 0 ] && sleep "$POLL_INTERVAL"
done
