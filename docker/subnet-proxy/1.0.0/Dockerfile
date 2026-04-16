FROM golang:1.25.9-alpine3.23 AS builder

WORKDIR /app
COPY go.mod .
COPY main.go .

RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o subnet-proxy .

FROM alpine:3.23

RUN apk add --no-cache ca-certificates
COPY --from=builder /app/subnet-proxy /usr/local/bin/subnet-proxy

EXPOSE 9999

ENTRYPOINT ["subnet-proxy"]
