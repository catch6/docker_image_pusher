# FFmpeg 视频转换服务

基于 Docker 的自动视频转换服务，监听输入目录，自动将视频转码为 H.264 MP4 格式（Web 播放优化），转换完成后输出到指定目录并删除源文件。

## 功能特性

- 自动监听 `/app/input` 目录，发现视频文件即开始转换
- H.264 编码，1080P 最高分辨率，Web 播放优化（`faststart`）
- Docker 资源限制 + `nice` 降优先级 + 线程控制，防止耗尽宿主机 CPU
- 文件稳定性检测，避免处理写入中的文件
- 容器重启自动恢复未完成任务
- 转换失败文件标记为 `.failed.*`，不会反复重试

## 快速开始

```bash
# 启动服务
docker compose up -d

# 放入视频文件，自动转换
cp video.mkv ./input/

# 转换完成后在 output 目录获取结果
ls ./output/
```

## 目录结构

```
ffmpeg-converter/
├── Dockerfile
├── docker-compose.yml
├── convert.sh
├── input/          # 放入待转换的视频文件
├── output/         # 转换完成的文件
└── logs/           # 日志文件
```

## FFmpeg 转码参数

```
-c:v libx264        H.264 编码
-preset slow         慢速编码，压缩率更高
-crf 20              高质量（适合 Web）
-profile:v high      High Profile
-level 4.1           支持 1080P
-movflags +faststart 浏览器无需下载完即可播放
-c:a aac -b:a 128k  AAC 音频 128kbps
-ac 2                双声道
```

分辨率处理：超过 1080P 自动缩放，低于 1080P 保持原分辨率。

## 环境变量

| 变量             | 默认值 | 说明                                            |
| ---------------- | ------ | ----------------------------------------------- |
| `FFMPEG_THREADS` | `2`    | ffmpeg 编码线程数                               |
| `CRF`            | `20`   | 质量控制，18=接近无损，20=Web 高清，23=Web 常规 |
| `POLL_INTERVAL`  | `5`    | 目录扫描间隔（秒）                              |

## 资源限制

| 层面        | 措施         | 默认值     |
| ----------- | ------------ | ---------- |
| Docker CPU  | `cpus`       | 2 核       |
| Docker 内存 | `mem_limit`  | 2 GB       |
| ffmpeg 线程 | `-threads`   | 2          |
| 进程优先级  | `nice -n 19` | 最低优先级 |

可在 `docker-compose.yml` 中根据宿主机配置调整资源限制。

## 支持的视频格式

mp4、mkv、avi、mov、wmv、flv、webm、ts、m4v、mpg、mpeg、3gp

## 日志

```bash
# Docker 日志
docker logs -f ffmpeg-converter

# 文件日志
tail -f ./logs/convert.log
```

日志示例：

```
[2026-02-19 10:00:00] FFmpeg 视频转换服务启动
[2026-02-19 10:00:00] 线程数: 2 | CRF: 20 | 轮询间隔: 5s
[2026-02-19 10:00:05] 开始转换: video.mkv
[2026-02-19 10:02:30] 转换完成: video.mkv -> video.mp4
```

## 故障处理

| 情况         | 行为                                         |
| ------------ | -------------------------------------------- |
| 转换失败     | 源文件重命名为 `*.failed.*`，需人工检查      |
| 容器重启     | 自动恢复 processing 目录中的中断文件         |
| 文件写入中   | 检测到文件大小仍在变化时跳过，下次轮询再检查 |
| 输出文件重名 | 自动追加时间戳避免覆盖                       |
