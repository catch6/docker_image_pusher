FROM registry.cn-beijing.aliyuncs.com/catch-public/alpine:latest

WORKDIR /app

RUN apk add --no-cache ffmpeg inotify-tools bash

COPY . .

RUN chmod +x /app/entrypoint.sh

ENV INPUTS="/app/input"
