FROM caddy:2.10.0-builder AS builder

RUN go env -w GOPROXY=https://goproxy.cn,direct

RUN xcaddy build \
  --with github.com/caddy-dns/cloudflare \
  --with github.com/caddy-dns/alidns \
  --with github.com/caddy-dns/tencentcloud

FROM caddy:2.10.0

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
