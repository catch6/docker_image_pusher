# mqtt-to-redis

轻量 MQTT → Redis 桥接服务，单文件 Rust 实现。MQTT 接收与 Redis 写入分别运行在异步任务中，通过有界内存队列衔接。默认每 500ms 或攒满 500 条批量写入 Redis hash。

镜像内运行无需安装 Rust，但需要可访问的 MQTT broker 和 Redis 服务。

## 工作方式

1. 先连接 Redis，成功后连接 MQTT 并订阅 `mqtt.topic`。Redis 首连未成功期间，不接收 MQTT 消息。
2. MQTT 消息的 topic 和原始 payload 进入队列。默认最多保留 100 万条待写消息，满时丢最旧消息，不限制字节数。
3. 独立写入任务解析 payload，按队列顺序执行 Redis pipeline。达到批量条数时写完整批次，剩余不足一批的消息在下一次定时刷写时处理。无消息时不发写入命令。

满批触发不会重置定时器；每个 pipeline 最多包含 `batch_size` 条消息。Redis 写入等待期间，MQTT 可继续接收并入队，但写入任务必须先完成当前批次/本轮刷写，再处理后续触发。因此 **500ms 是刷写间隔，不是端到端延迟上限**。

## 消息与 Redis 数据

payload 示例：

```json
{
  "time": "2026-03-07 14:30:00",
  "properties": [
    { "code": "temp", "value": 25.6 },
    { "code": "mode", "value": 2 }
  ]
}
```

- `time`：必填字符串，不校验时间格式，也不用于判断消息新旧。
- `properties`：可省略，省略时只写 `time`；提供时必须是数组，不能为 `null`。
- 每项 `code`：必填字符串；`value` 缺失或为 `null` 时跳过该属性。
- 字符串值解码后原样存储；数字保留原始文本、精度、尾零和指数；布尔、数组、对象存为 JSON 文本。
- JSON 或任一属性结构非法时，跳过整条消息并记录错误，不中断其他消息。

Redis **key** 为 topic 中的 `/` 替换成 `:`，如 `a/b/c` → `a:b:c`。topic 命名应避免映射冲突，例如 `a/b:c` 和 `a:b/c` 会写入同一个 key。

每条消息使用 `HSET` 写入 `time` 和有效属性。这是**增量合并更新**：未上报或跳过的属性不删除旧字段，不设置过期时间；重复 `code` 后者覆盖前者，属性 `code="time"` 也会覆盖顶层时间。同值重复写入不改变字段值，但旧消息或乱序消息可能覆盖新状态，程序不去重、不按时间排序。

## 配置

读取运行时当前目录的 `config.toml`。模板位于 `1.0.0/config.example.toml`，镜像内为 `/app/config.example.toml`。

```toml
[mqtt]
host = "broker.example.com"
port = 1883
client_id = "mqtt-to-redis"
username = "<username>"
password = "<password>"
topic = "a/+/b/+/data"
qos = 1

[redis]
host = "redis.example.com"
port = 6379
password = "<password>"
db = 0

[buffer]
max_messages = 1000000             # 待写队列容量
flush_interval_ms = 500            # 刷写间隔，毫秒
batch_size = 500                   # 满批触发条数，也是单批上限
```

- `[buffer]` 整段或任意属性均可省略，缺失属性采用上述默认值；三个值均须大于 0。不支持字节容量限制。
- MQTT 所有字段均必填，匿名连接也需填写 `username = ""`、`password = ""`；用户名为空时不设置认证。`qos` 仅接受 0、1、2；示例中的 1 不是默认值。
- MQTT 接收上限为 1 MiB（1048576 字节），按 Remaining Length 计算，包含 topic、包标识符（QoS 1/2）和 payload；发送上限保持 10 KiB。超限消息会导致断线，超限保留消息可能导致持续重连。
- `redis.host`、`redis.port`、`redis.db` 必填；`redis.password` 可省略，默认空字符串。密码按原始值填写，无需 URL 编码。
- `topic` 支持 MQTT 的 `+`、`#` 通配符。多实例的 `client_id` 必须唯一，否则会互相断开连接；使用相同普通订阅会各自收到消息，不自动分摊负载。
- MQTT 和 Redis 固定使用明文 TCP。两者 `host` 必须是非空纯主机名或 IP，不带协议、路径、端口或方括号；IPv6 填写裸地址，如 `::1`。`port` 必填，必须是 1–65535 的整数，无默认端口。不兼容旧 `mqtt.broker`、`redis.addr` 配置，缺失 `host` 或 `port` 会启动失败。

队列按实际消息量增长，不预分配 100 万条。100 万条 × 200 字节仅 payload 约 200 MB，topic、队列结构、内存分配、写入中的批次和运行时另占内存。容量只约束待写队列；持续输入快于 Redis 写入时，最终仍会淘汰旧消息。

## 可靠性与日志

- **MQTT 断线**：等待 1 秒后重连，连接成功后重新订阅。收到订阅成功响应才记录成功；订阅被拒绝或响应异常时退出并返回 1。
- **Redis 断线**：首连每次最多等待 3 秒，失败按 1s→30s 退避重试；连接建立后的重连由 ConnectionManager 处理。
- **Redis 写失败**：本批不重放，记录错误及“写入未确认”数量。pipeline 不是事务，部分命令可能已执行；该计数不表示整批都未写入。
- **退出**：SIGINT/SIGTERM 停止接收，尽力写出队列；最多等待 5 秒，包含等待当前写入的时间，超时退出码为 1。积压较大时不能保证排空；退出码为 0 也不代表所有消息都写入确认。
- **持久性**：队列仅在内存中。淘汰、进程崩溃、强制终止或退出超时都可能丢失消息。MQTT QoS 1 不等于 Redis 持久化保证。
- **日志**：每 10 秒及正常收尾时汇总接收、写入确认、非法、丢弃、写入未确认、待写和写入中条数，不逐条打印成功消息。独立日志线程最多缓存 256 条，日志队列满时丢弃新日志。

## 构建与运行

当前版本为 `1.0.0`，目录为 `1.0.0/`。以下从本 README 所在目录开始，使用本地构建的镜像，不依赖镜像已发布：

```bash
docker build -t mqtt-to-redis:1.0.0 ./1.0.0
docker run --rm --entrypoint cat mqtt-to-redis:1.0.0 /app/config.example.toml > config.toml
```

编辑 `config.toml` 后启动：

```bash
docker run -d --name mqtt-to-redis \
  -v "$PWD/config.toml:/app/config.toml:ro" \
  mqtt-to-redis:1.0.0
```

CI 在 main 分支的 `docker/**` 或 `docker.txt` 变更时触发，再按版本目录的变更筛选构建；仅修改本 README 通常不会构建镜像。手动触发构建全部版本。默认构建 `linux/amd64`、`linux/arm64`，可由 `docker.txt` 覆盖平台。

发布标签为 `${DOCKERHUB_USERNAME}/mqtt-to-redis:1.0.0` 和 `${ALIYUN_REGISTRY}/${ALIYUN_NAME_SPACE}/mqtt-to-redis:1.0.0`，不发布 `latest`。使用远端镜像时，将上述本地镜像名替换为实际发布地址。CI 不执行下列测试；发布后应核实目标仓库标签，不能仅凭 CI 绿色判断发布成功。

## 测试

在 `1.0.0/` 下运行。集成测试只需 Python 3 标准库及本机回环端口，无需外部 MQTT 或 Redis 服务：

```bash
cargo test --locked
cargo build --locked
python3 tests/integration.py
```

## 压测

`1.0.0/tests/load_test.py` 启动独立、仅监听回环地址的真实 Redis 和 Mosquitto，关闭持久化，结束后清理测试服务。PATH 中需有 `redis-server`、`mosquitto`、`python3`、`ps`；资源采样已在 macOS 上验证。

在 `1.0.0/` 下运行，每次使用新的输出目录：

```bash
cargo build --release --locked
python3 tests/load_test.py --suite smoke --output /tmp/mqtt-load-smoke
python3 tests/load_test.py --suite full --output /tmp/mqtt-load-full
```

完整测试包含持续流量、大消息、Redis 响应延迟和突发流量。结果记录在 `results.json` 和各场景的 `result.json`，包括收发计数、延迟、CPU、RSS、最终设备状态及 `checks`。

**必须检查所有 `checks`，脚本退出 0 不代表全部通过。** 慢响应场景停止发送后仅等待固定时间，再触发程序的 5 秒退出排空；仍有积压时可能超时退出并丢失剩余队列，应区分运行期间淘汰和测试结束造成的未交付。

响应代理延迟 Redis 返回数据，不模拟逐条命令执行耗时。端到端延迟每 200 条采样一次，通过独立 key、约 50ms 轮询观察，因此包含观察器延迟。`missing_samples` 是结束时未观测到的样本数，不能直接当作丢弃数。共享本机压测结果不能直接外推到生产网络、容器或开启持久化后的容量。
