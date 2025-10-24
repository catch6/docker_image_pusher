#!/usr/bin/env bash
# entrypoint.sh
# 轻量化视频批量转码守护脚本
# 需求要点：
# - 多输入目录：环境变量 INPUTS="/in1,/data/in2"
# - 忽略规则：IGNORES="*.wmv,**/*.wmv"（应用于完整路径）
# - 输出目录：/output（可用 OUTPUT_DIR 覆盖）
# - 目标：H.264 MP4，CRF=22，最高 1920x1080，不上采样；转码进度、faststart、完成后删除源
# - 持续监听：优先用 inotify；不可用则退化为轮询
# - 重启后：跳过已完成（有对应 .mp4）文件，未完成则重头转

set -Eeuo pipefail
IFS=$'\n\t'

#######################################
# 基础工具与日志
#######################################
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

die() { log "ERROR: $*"; exit 1; }

trap 'log "收到停止信号，正在退出..."; exit 0' INT TERM

#######################################
# 读取环境变量
#######################################
INPUTS="${INPUTS:-}"
OUTPUT_DIR="${OUTPUT_DIR:-/output}"
IGNORES="${IGNORES:-}"
SLEEP_SECONDS="${SLEEP_SECONDS:-5}"
INOTIFY_MODE="${INOTIFY_MODE:-auto}"  # auto|on|off
DELETE_SOURCE="${DELETE_SOURCE:-1}"    # 1 删除源文件；0 保留
VIDEO_EXTS=(mp4 mov avi mkv flv wmv rmvb rm)

[[ -z "$INPUTS" ]] && die "必须设置 INPUTS 环境变量，如：/input1,/data/input2"

# 解析 CSV 列表为数组
IFS=',' read -r -a INPUT_DIRS <<< "$INPUTS"
if [[ ${#INPUT_DIRS[@]} -eq 0 ]]; then die "INPUTS 为空"; fi

# 规范化输入目录（去掉末尾的 /）并校验存在性
for i in "${!INPUT_DIRS[@]}"; do
  d="${INPUT_DIRS[$i]}"
  d="${d%/}"
  [[ -z "$d" ]] && continue
  if [[ ! -d "$d" ]]; then die "输入目录不存在：$d"; fi
  INPUT_DIRS[$i]="$d"
done

mkdir -p "$OUTPUT_DIR"

# 解析忽略模式
IGNORE_PATTERNS=()
if [[ -n "$IGNORES" ]]; then
  IFS=',' read -r -a IGNORE_PATTERNS <<< "$IGNORES"
fi

#######################################
# 工具函数
#######################################
lower() { awk '{print tolower($0)}' <<<"$1"; }

is_video_file() {
  local p lc ext
  p="$1"; lc="$(lower "$p")"
  for ext in "${VIDEO_EXTS[@]}"; do
    [[ "$lc" == *".${ext}" ]] && return 0
  done
  return 1
}

matches_ignore() {
  local path="$1" pat trimmed
  [[ ${#IGNORE_PATTERNS[@]} -eq 0 ]] && return 1
  for pat in "${IGNORE_PATTERNS[@]}"; do
    # 去空白
    trimmed="${pat#"${pat%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ -z "$trimmed" ]] && continue
    # case 模式匹配对完整路径生效（* 匹配 /）
    case "$path" in
      $trimmed) return 0 ;;
    esac
  done
  return 1
}

# 判断文件是否“稳定”（大小 1 秒内未变化，避免半写入文件）
is_file_stable() {
  local f="$1" s1 s2
  [[ ! -f "$f" ]] && return 1
  s1=$(stat -c%s -- "$f" 2>/dev/null || echo "")
  [[ -z "$s1" ]] && return 1
  sleep 1
  s2=$(stat -c%s -- "$f" 2>/dev/null || echo "")
  [[ -z "$s2" ]] && return 1
  [[ "$s1" == "$s2" ]]
}

# 根据输入文件生成输出 mp4 路径（在 /output 下保留输入目录名+相对路径，避免重名冲突）
# 例：/input1/sub/a.mkv -> /output/input1/sub/a.mp4
output_path_for() {
  local src="$1" in root rel base out
  for in in "${INPUT_DIRS[@]}"; do
    if [[ "$src" == "$in"/* || "$src" == "$in" ]]; then
      root="$in"
      rel="${src#$root/}"
      base="$(basename "$root")"
      rel="${rel%.*}.mp4"
      out="$OUTPUT_DIR/$base/$rel"
      echo "$out"
      return 0
    fi
  done
  # 不在任何输入目录（理论上不该发生）
  local fname; fname="$(basename "${src%.*}").mp4"
  echo "$OUTPUT_DIR/$fname"
}

# 获取视频总时长（微秒，int）。若失败返回空
get_duration_us() {
  local f="$1" dur
  dur="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 -- "$f" 2>/dev/null || true)"
  if [[ -z "$dur" || "$dur" == "N/A" || "$dur" == "0" ]]; then
    dur="$(ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=nw=1:nk=1 -- "$f" 2>/dev/null || true)"
  fi
  if [[ -z "$dur" || "$dur" == "N/A" || "$dur" == "0" ]]; then
    echo ""
    return 1
  fi
  awk -v d="$dur" 'BEGIN{printf("%.0f\n", d*1000000)}'
}

# 检查是否有音频流
has_audio_stream() {
  local f="$1" a
  a="$(ffprobe -v error -select_streams a -show_entries stream=codec_type -of csv=p=0 -- "$f" 2>/dev/null || true)"
  [[ -n "$a" ]]
}

# 执行一次转码（包含进度显示），成功后删除源
transcode_one() {
  local src="$1"
  local out tmp dir duration_us last_p p_int line key val out_ms speed fps
  local -a audio_args

  # 忽略规则 / 扩展名
  if ! is_video_file "$src"; then return 0; fi
  if matches_ignore "$src"; then
    log "忽略(匹配 IGNORE)：$src"
    return 0
  fi
  if [[ ! -f "$src" ]]; then return 0; fi
  if ! is_file_stable "$src"; then
    log "文件未稳定，稍后再试：$src"
    return 0
  fi

  out="$(output_path_for "$src")"
  tmp="${out}.partial"
  dir="$(dirname "$out")"
  mkdir -p -- "$dir"

  # 如果已存在最终输出文件，视为已完成；删除源并跳过
  if [[ -f "$out" ]]; then
    log "检测到已完成输出，跳过并删除源：$src -> $out"
    [[ "$DELETE_SOURCE" -eq 1 ]] && rm -f -- "$src" || true
    return 0
  fi

  # 清理历史残留的 partial
  [[ -f "$tmp" ]] && rm -f -- "$tmp" || true

  # 音频参数：有音频则编码 AAC；无音频则 -an
  if has_audio_stream "$src"; then
    audio_args=(-c:a aac -b:a 128k)
  else
    audio_args=(-an)
  fi

  # 进度基准
  duration_us="$(get_duration_us "$src" || true)"
  last_p=-1

  log "开始转码：$src -> $out"

  # 说明：
  # - 限制分辨率不超过 1920x1080，保持比例且不升采样（force_original_aspect_ratio=decrease + force_divisible_by=2）
  # - 24fps，下采样帧率以减小体积
  # - H.264 + CRF 22 + slower + tune film + high profile + yuv420p + faststart
  # - 使用 -progress pipe:1 输出可解析的进度
  set +e
  ffmpeg -hide_banner -y -i "$src" \
    -vf "scale=w=1920:h=1080:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=24" \
    -c:v libx264 -crf 22 -preset slower -tune film -profile:v high -pix_fmt yuv420p \
    "${audio_args[@]}" -movflags +faststart \
    -progress pipe:1 -nostats -loglevel error \
    "$tmp" \
  | while IFS='=' read -r key val; do
      case "$key" in
        out_time_ms)
          out_ms="$val"
          if [[ -n "${duration_us:-}" && "$duration_us" -gt 0 ]]; then
            p_int="$(awk -v t="$out_ms" -v d="$duration_us" 'BEGIN{ if (d>0){ printf("%d", (t*100)/d) } else { print -1 } }')"
            if [[ "$p_int" -gt "$last_p" ]]; then
              last_p="$p_int"
              # 打印到 1% 粒度（避免过于频繁）
              log "进度：${p_int}% - $src"
            fi
          else
            # 无法获取总时长，退化为时间戳进度（另有 out_time 可解析，但此处已足够）
            :
          fi
          ;;
        speed)
          speed="$val" # 可用于额外输出
          ;;
        fps)
          fps="$val"
          ;;
        progress)
          if [[ "$val" == "end" ]]; then
            log "进度：100% - $src"
          fi
          ;;
      esac
    done
  rc=$?
  set -e

  if [[ $rc -ne 0 ]]; then
    log "转码失败：$src（退出码 $rc）"
    [[ -f "$tmp" ]] && rm -f -- "$tmp" || true
    return $rc
  fi

  mv -f -- "$tmp" "$out"
  log "完成转码：$src -> $out"

  if [[ "$DELETE_SOURCE" -eq 1 ]]; then
    rm -f -- "$src" || true
    log "已删除源文件：$src"
  fi
}

# 扫描全部输入目录，按扩展名找视频
scan_all_once() {
  local d f
  for d in "${INPUT_DIRS[@]}"; do
    # 仅匹配我们支持的扩展名（大小写不敏感）
    # 使用 find 可以避免 shell 展开带来的性能问题
    while IFS= read -r -d '' f; do
      transcode_one "$f" || true
    done < <(find "$d" -type f \( \
               -iname "*.mp4" -o -iname "*.mov" -o -iname "*.avi" -o \
               -iname "*.mkv" -o -iname "*.flv" -o -iname "*.wmv" -o \
               -iname "*.rmvb" -o -iname "*.rm" \) -print0 | sort -z)
  done
}

#######################################
# 启动阶段：清理残留、初次扫描
#######################################
log "启动。输入目录：${INPUT_DIRS[*]} 输出目录：$OUTPUT_DIR"
[[ ${#IGNORE_PATTERNS[@]} -gt 0 ]] && log "忽略规则：${IGNORE_PATTERNS[*]}"

# 清理历史 partial
find "$OUTPUT_DIR" -type f -name "*.partial" -print0 2>/dev/null | xargs -0r rm -f -- || true

# 初次扫描（容器重启后可处理停机期间积累的文件）
scan_all_once

#######################################
# 监听/轮询 主循环
#######################################
use_inotify=false
if [[ "$INOTIFY_MODE" != "off" ]] && command -v inotifywait >/dev/null 2>&1; then
  use_inotify=true
fi

if $use_inotify; then
  log "使用 inotify 持续监听新增/写入完成/移动进入的文件事件..."
  # -m 持续；-r 递归；close_write/moved_to 代表写入完成或被移动到目录中
  inotifywait -m -r -e close_write -e moved_to --format '%w%f' "${INPUT_DIRS[@]}" \
  | while read -r path; do
      [[ -f "$path" ]] || continue
      transcode_one "$path" || true
    done
else
  log "inotify 不可用或关闭，进入轮询模式（每 ${SLEEP_SECONDS}s 扫描一次）..."
  while true; do
    scan_all_once
    sleep "$SLEEP_SECONDS"
  done
fi
