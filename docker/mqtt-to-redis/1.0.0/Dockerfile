FROM rust:1.98-alpine3.24 AS builder

WORKDIR /app

# 依赖预热层：仅 Cargo.toml 时以空壳 main 编译依赖，源码变动不触发依赖重编
COPY Cargo.toml Cargo.lock* ./
RUN echo "fn main() {}" > main.rs && cargo build --release && rm main.rs

COPY main.rs main.rs
RUN touch main.rs && cargo build --release

FROM alpine:3.24

RUN apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone
ENV TZ=Asia/Shanghai

WORKDIR /app
COPY --from=builder /app/target/release/mqtt-to-redis /app/mqtt-to-redis
COPY config.example.toml /app/config.example.toml

# 镜像包含配置模板；运行时仍需挂载填写好的 config.toml 到 /app/config.toml
CMD ["/app/mqtt-to-redis"]
