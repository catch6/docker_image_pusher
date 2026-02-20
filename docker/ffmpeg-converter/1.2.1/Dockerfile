FROM alpine:3

LABEL maintainer="Catch"
LABEL description="FFmpeg H.264 视频转换服务 - 监听目录自动转码（Web 优化）"

RUN apk add --no-cache ffmpeg bash coreutils

RUN mkdir -p /app/input /app/output /app/processing /app/logs

COPY convert.sh /app/convert.sh
RUN chmod +x /app/convert.sh

VOLUME ["/app/input", "/app/output", "/app/processing", "/app/logs"]

ENV FFMPEG_THREADS=2
ENV CRF=23
ENV POLL_INTERVAL=5

ENTRYPOINT ["/app/convert.sh"]
