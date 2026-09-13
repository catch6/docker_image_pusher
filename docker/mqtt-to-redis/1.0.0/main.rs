// mqtt-to-redis —— 通用 MQTT → Redis 桥接。
//
// 订阅 mqtt.topic，把每条消息的 payload 同步到 Redis hash：
// key = 消息 topic 的 '/' 替换为 ':'（如 a/b/c → a:b:c），
// 字段 = time + 各属性码，值转字符串（数字原文本 / 字符串原样 / bool）。
// QoS1 重复投递由 HSET 天然幂等吸收（同消息重放同值）。
// 满批或到达刷新间隔时顺序写入；缓冲区满时丢最旧消息，写失败不重放旧批次。

use std::collections::VecDeque;
use std::process;
use std::sync::{Arc, Mutex, OnceLock, mpsc};

use redis::aio::ConnectionManager;
use rumqttc::{AsyncClient, Event, MqttOptions, Packet, QoS, SubscribeReasonCode};
use serde::Deserialize;
use serde_json::value::RawValue;
use tokio::time::{Duration, Instant, MissedTickBehavior, interval_at, sleep};
use tokio::{
    signal,
    sync::{Notify, watch},
};

// ========== 配置 ==========

#[derive(Deserialize)]
struct Config {
    mqtt: MqttConfig,
    redis: RedisConfig,
    #[serde(default)]
    buffer: BufferConfig,
}

#[derive(Clone, Deserialize)]
#[serde(default)]
struct BufferConfig {
    max_messages: usize,
    flush_interval_ms: u64,
    batch_size: usize,
}

impl Default for BufferConfig {
    fn default() -> Self {
        Self {
            max_messages: 1_000_000,
            flush_interval_ms: 500,
            batch_size: 500,
        }
    }
}

#[derive(Deserialize)]
struct MqttConfig {
    host: String,
    port: u16,
    client_id: String,
    username: String,
    password: String,
    topic: String,
    qos: u8,
}

#[derive(Deserialize)]
struct RedisConfig {
    host: String,
    port: u16,
    #[serde(default)]
    password: String,
    db: i64,
}

fn load_config() -> Result<Config, String> {
    let raw = std::fs::read_to_string("config.toml")
        .map_err(|e| format!("读取 config.toml 失败: {e}"))?;
    let cfg: Config = toml::from_str(&raw).map_err(|e| format!("解析 config.toml 失败: {e}"))?;
    for (name, host, port) in [
        ("mqtt", cfg.mqtt.host.as_str(), cfg.mqtt.port),
        ("redis", cfg.redis.host.as_str(), cfg.redis.port),
    ] {
        let hostname = host.strip_suffix('.').unwrap_or(host);
        let valid_hostname = host.len() <= 253
            && hostname.split('.').all(|label| {
                !label.is_empty()
                    && label.len() <= 63
                    && !label.starts_with('-')
                    && !label.ends_with('-')
                    && label
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            });
        if host.parse::<std::net::IpAddr>().is_err() && !valid_hostname {
            return Err(format!(
                "{name}.host 必须是非空纯主机名或 IP，不带协议、路径、端口或方括号"
            ));
        }
        if port == 0 {
            return Err(format!("{name}.port 必须大于 0"));
        }
    }
    if cfg.buffer.max_messages == 0
        || cfg.buffer.flush_interval_ms == 0
        || cfg.buffer.batch_size == 0
    {
        return Err(
            "buffer.max_messages、buffer.flush_interval_ms 和 buffer.batch_size 必须大于 0".into(),
        );
    }
    Ok(cfg)
}

fn redis_client(cfg: &RedisConfig) -> redis::RedisResult<redis::Client> {
    // redis 1 的 ConnectionInfo 字段私有，通过 TCP 地址创建配置，不解析 URL 或发起连接。
    let client = redis::Client::open(redis::ConnectionAddr::Tcp(cfg.host.clone(), cfg.port))?;
    let info = client.get_connection_info().clone();
    let mut settings = redis::RedisConnectionInfo::default().set_db(cfg.db);
    if !cfg.password.is_empty() {
        settings = settings.set_password(&cfg.password);
    }
    redis::Client::open(info.set_redis_settings(settings))
}

// ========== 消息与值转换 ==========

#[derive(serde::Deserialize)]
struct DeviceData {
    time: String,
    #[serde(default)]
    properties: Vec<Property>,
}

#[derive(serde::Deserialize)]
struct Property {
    code: String,
    #[serde(default, deserialize_with = "deserialize_property_value")]
    value: Option<String>,
}

// 数字保留原始 token（精度、尾零、指数）；字符串仍按 JSON 规则解码。
fn deserialize_property_value<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let raw = Box::<RawValue>::deserialize(deserializer)?;
    let text = raw.get();
    match text.as_bytes()[0] {
        b'n' => Ok(None),
        b'"' => serde_json::from_str::<String>(text)
            .map(Some)
            .map_err(serde::de::Error::custom),
        // 布尔和复杂类型也保留合法 JSON 文本。
        _ => Ok(Some(text.to_owned())),
    }
}

// ========== 日志 ==========

enum LogEntry {
    Line(String),
    Flush(mpsc::SyncSender<()>),
}

// 单独线程输出日志；收集端卡住时最多积压 256 条，不能拖住消息转发。
fn logger() -> &'static mpsc::SyncSender<LogEntry> {
    static LOGGER: OnceLock<mpsc::SyncSender<LogEntry>> = OnceLock::new();
    LOGGER.get_or_init(|| {
        let (tx, rx) = mpsc::sync_channel(256);
        std::thread::spawn(move || {
            for entry in rx {
                match entry {
                    LogEntry::Line(line) => println!("{line}"),
                    LogEntry::Flush(done) => {
                        let _ = done.send(());
                    }
                }
            }
        });
        tx
    })
}

fn log(level: &str, msg: &str) {
    let _ = logger().try_send(LogEntry::Line(format!(
        "{} [{}] {}",
        chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f"),
        level,
        msg
    )));
}

fn flush_logs() {
    let (tx, rx) = mpsc::sync_channel(1);
    if logger().try_send(LogEntry::Flush(tx)).is_ok() {
        let _ = rx.recv_timeout(Duration::from_millis(200));
    }
}

// ========== 有界消息缓冲 ==========

const LOG_INTERVAL: Duration = Duration::from_secs(10);

struct Message {
    topic: String,
    payload: Vec<u8>,
}

#[derive(Default)]
struct Stats {
    received: u64,
    written: u64,
    invalid: u64,
    dropped: u64,
    unconfirmed: u64,
}

struct MessageBuffer {
    limits: BufferConfig,
    messages: VecDeque<Message>,
    notify: Arc<Notify>,
    inflight: usize,
    stats: Stats,
}

impl MessageBuffer {
    fn new(limits: BufferConfig) -> Self {
        Self {
            limits,
            messages: VecDeque::new(),
            notify: Arc::new(Notify::new()),
            inflight: 0,
            stats: Stats::default(),
        }
    }

    fn push(&mut self, message: Message) {
        self.stats.received += 1;
        if self.messages.len() >= self.limits.max_messages {
            self.messages.pop_front();
            self.stats.dropped += 1;
        }
        self.messages.push_back(message);
        if self.messages.len() >= self.limits.batch_size {
            self.notify.notify_one();
        }
    }

    fn take_batch(&mut self, limit: usize) -> Vec<Message> {
        let count = limit.min(self.limits.batch_size).min(self.messages.len());
        let batch: Vec<_> = self.messages.drain(..count).collect();
        self.inflight = batch.len();
        batch
    }
}

type SharedBuffer = Arc<Mutex<MessageBuffer>>;

fn report_stats(buffer: &SharedBuffer) {
    let buffer = buffer.lock().unwrap();
    let s = &buffer.stats;
    log(
        "INFO",
        &format!(
            "[Stats] 累计接收={} 写入确认={} 非法={} 丢弃={} 写入未确认={} 待写={} 写入中={}",
            s.received,
            s.written,
            s.invalid,
            s.dropped,
            s.unconfirmed,
            buffer.messages.len(),
            buffer.inflight
        ),
    );
}

async fn report_periodically(buffer: SharedBuffer) {
    let mut interval = interval_at(Instant::now() + LOG_INTERVAL, LOG_INTERVAL);
    interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        interval.tick().await;
        report_stats(&buffer);
    }
}

async fn write_periodically(
    mut conn: ConnectionManager,
    buffer: SharedBuffer,
    mut shutdown: watch::Receiver<bool>,
) {
    let (limits, notify) = {
        let buffer = buffer.lock().unwrap();
        (buffer.limits.clone(), buffer.notify.clone())
    };
    let period = Duration::from_millis(limits.flush_interval_ms);
    let mut interval = interval_at(Instant::now() + period, period);
    interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            biased;
            _ = shutdown.changed() => {
                flush_buffer(&mut conn, &buffer, true).await;
                return;
            }
            _ = interval.tick() => flush_buffer(&mut conn, &buffer, true).await,
            _ = async {
                // 单 writer：通知可合并，队列长度才是依据；过期通知不刷写尾部。
                loop {
                    if buffer.lock().unwrap().messages.len() >= limits.batch_size {
                        break;
                    }
                    notify.notified().await;
                }
            } => flush_buffer(&mut conn, &buffer, false).await,
        }
    }
}

async fn flush_buffer(conn: &mut ConnectionManager, buffer: &SharedBuffer, flush_partial: bool) {
    // 限制本轮工作量；数量触发只取完整批次，定时和退出刷完快照。
    let mut remaining = {
        let buffer = buffer.lock().unwrap();
        let count = buffer.messages.len();
        if flush_partial {
            count
        } else {
            count - count % buffer.limits.batch_size
        }
    };
    while remaining > 0 {
        let batch = buffer.lock().unwrap().take_batch(remaining);
        if batch.is_empty() {
            break;
        }
        remaining -= batch.len();
        let mut pipeline = redis::pipe();
        let mut count = 0;
        let mut invalid = 0;
        let mut first_error = None;
        for message in batch {
            match message_command(&message) {
                Ok(command) => {
                    pipeline.add_command(command);
                    count += 1;
                }
                Err(e) => {
                    invalid += 1;
                    first_error.get_or_insert(e);
                }
            }
        }
        if let Some(e) = first_error {
            log(
                "ERROR",
                &format!("[MQTT] 本批跳过 {invalid} 条非法消息，首个解析错误: {e}"),
            );
        }
        {
            let mut buffer = buffer.lock().unwrap();
            buffer.stats.invalid += invalid;
            buffer.inflight = count;
        }
        if count > 0 {
            match pipeline.query_async::<Vec<redis::Value>>(conn).await {
                Ok(_) => buffer.lock().unwrap().stats.written += count as u64,
                Err(e) => {
                    buffer.lock().unwrap().stats.unconfirmed += count as u64;
                    log(
                        "ERROR",
                        &format!(
                            "[Redis] 批量写入未完全确认: messages={count} err={e}；可能部分已写入，本批不重试"
                        ),
                    );
                }
            }
        }
        buffer.lock().unwrap().inflight = 0;
    }
}

// ========== 主逻辑 ==========

#[tokio::main]
async fn main() {
    let cfg = match load_config() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[Config] {e}");
            process::exit(1);
        }
    };
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);

    // Redis：ConnectionManager 断线自动重连
    let client = match redis_client(&cfg.redis) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[Config] Redis 地址非法: {e}");
            process::exit(1);
        }
    };
    let conn = tokio::select! {
        _ = &mut shutdown => { flush_logs(); return; }
        conn = connect_redis(client) => conn,
    };
    log("INFO", "[Redis] 连接成功");

    let qos = match cfg.mqtt.qos {
        0 => QoS::AtMostOnce,
        1 => QoS::AtLeastOnce,
        2 => QoS::ExactlyOnce,
        n => {
            eprintln!("[Config] mqtt.qos 非法: {n}");
            process::exit(1);
        }
    };

    // MQTT：EventLoop poll 驱动，断线自动重连，ConnAck 后重新订阅
    let mut opts = MqttOptions::new(&cfg.mqtt.client_id, &cfg.mqtt.host, cfg.mqtt.port);
    opts.set_keep_alive(Duration::from_secs(30));
    opts.set_max_packet_size(1024 * 1024, 10 * 1024);
    if !cfg.mqtt.username.is_empty() {
        opts.set_credentials(&cfg.mqtt.username, &cfg.mqtt.password);
    }

    let (client, mut eventloop) = AsyncClient::new(opts, 256);
    let topic = cfg.mqtt.topic.clone();
    let buffer = Arc::new(Mutex::new(MessageBuffer::new(cfg.buffer)));
    let (stop_writer, writer_shutdown) = watch::channel(false);
    let mut writer = tokio::spawn(write_periodically(conn, buffer.clone(), writer_shutdown));
    let reporter = tokio::spawn(report_periodically(buffer.clone()));

    let exit_code = loop {
        tokio::select! {
            _ = &mut shutdown => {
                break 0;
            }
            result = &mut writer => {
                log("ERROR", &format!("[Redis] 写入任务意外退出: {result:?}"));
                reporter.abort();
                flush_logs();
                process::exit(1);
            }
            event = eventloop.poll() => match event {
                Ok(Event::Incoming(Packet::ConnAck(ack))) => {
                    if ack.code == rumqttc::ConnectReturnCode::Success {
                        log("INFO", &format!("[MQTT] 已连接，正在请求订阅 {topic}"));
                        if let Err(e) = client.subscribe(&topic, qos).await {
                            log("ERROR", &format!("[MQTT] 订阅失败: {e}"));
                            break 1;
                        }
                    }
                }
                Ok(Event::Incoming(Packet::SubAck(ack))) => {
                    // 本服务仅订阅一个主题，必须收到一个成功返回码。
                    match ack.return_codes.as_slice() {
                        [SubscribeReasonCode::Success(granted_qos)] => {
                            log("INFO", &format!("[MQTT] 订阅成功: {topic}, QoS={granted_qos:?}"));
                        }
                        codes => {
                            log("ERROR", &format!(
                                "[MQTT] 订阅被拒绝或响应异常: topic={topic}, 返回码={codes:?}；请检查主题与 broker ACL，修正后重启"
                            ));
                            break 1;
                        }
                    }
                }
                Ok(Event::Incoming(Packet::Publish(p))) => {
                    buffer.lock().unwrap().push(Message {
                        topic: p.topic,
                        payload: p.payload.to_vec(),
                    });
                }
                Ok(_) => {}
                Err(e) => {
                    log("ERROR", &format!("[MQTT] 连接异常，1s 后自动重连: {e}"));
                    tokio::select! {
                        _ = &mut shutdown => break 0,
                        _ = sleep(Duration::from_secs(1)) => {},
                    }
                }
            },
        }
    };
    drop(client);
    drop(eventloop);
    log("INFO", "[Shutdown] 已停止接收，最多等待 5s 写出剩余缓冲...");
    let _ = stop_writer.send(true);
    let mut exit_code = exit_code;
    match tokio::time::timeout(Duration::from_secs(5), &mut writer).await {
        Ok(Ok(())) => {}
        Ok(Err(e)) => {
            log("ERROR", &format!("[Shutdown] 写入任务失败: {e}"));
            exit_code = 1;
        }
        Err(_) => {
            writer.abort();
            let _ = writer.await;
            log("ERROR", "[Shutdown] 写出超时，剩余内存缓冲将丢失");
            exit_code = 1;
        }
    }
    reporter.abort();
    report_stats(&buffer);
    log("INFO", "[Shutdown] 已退出");
    flush_logs();
    if exit_code != 0 {
        process::exit(exit_code);
    }
}

async fn connect_redis(client: redis::Client) -> ConnectionManager {
    // 首连不可达时限时尝试，并按 1s→30s 退避；后续重连由 manager 负责。
    let mut backoff = 1u64;
    loop {
        match tokio::time::timeout(
            Duration::from_secs(3),
            ConnectionManager::new(client.clone()),
        )
        .await
        {
            Ok(Ok(conn)) => return conn,
            Ok(Err(e)) => log(
                "ERROR",
                &format!("[Redis] 连接失败，{backoff}s 后重试: {e}"),
            ),
            Err(_) => log("ERROR", &format!("[Redis] 连接超时，{backoff}s 后重试")),
        }
        sleep(Duration::from_secs(backoff)).await;
        backoff = (backoff * 2).min(30);
    }
}

fn message_command(message: &Message) -> Result<redis::Cmd, serde_json::Error> {
    let data: DeviceData = serde_json::from_slice(&message.payload)?;
    // key = 消息 topic 的 '/' 替换为 ':'，无任何硬编码结构
    let key = message.topic.replace('/', ":");
    let mut cmd = redis::cmd("HSET");
    cmd.arg(&key).arg("time").arg(&data.time);
    for prop in &data.properties {
        if let Some(v) = &prop.value {
            cmd.arg(&prop.code).arg(v);
        }
    }
    Ok(cmd)
}

async fn shutdown_signal() {
    let ctrl_c = signal::ctrl_c();
    #[cfg(unix)]
    {
        let mut term =
            signal::unix::signal(signal::unix::SignalKind::terminate()).expect("注册 SIGTERM 失败");
        tokio::select! {
            _ = ctrl_c => {},
            _ = term.recv() => {},
        }
    }
    #[cfg(not(unix))]
    {
        ctrl_c.await.ok();
    }
}

// ========== 自检 ==========

#[cfg(test)]
mod tests {
    use super::*;

    fn buffered_message(value: &str) -> Message {
        Message {
            topic: "a".into(),
            payload: value.as_bytes().to_vec(),
        }
    }

    #[test]
    fn buffer_config_defaults_and_partial_overrides() {
        let base = include_str!("config.example.toml")
            .split("[buffer]")
            .next()
            .unwrap();
        for (section, expected) in [
            ("", (1_000_000, 500, 500)),
            ("[buffer]", (1_000_000, 500, 500)),
            ("[buffer]\nmax_messages = 2", (2, 500, 500)),
            ("[buffer]\nflush_interval_ms = 25", (1_000_000, 25, 500)),
            ("[buffer]\nbatch_size = 3", (1_000_000, 500, 3)),
            (
                "[buffer]\nmax_messages = 7\nflush_interval_ms = 20\nbatch_size = 4",
                (7, 20, 4),
            ),
        ] {
            let cfg: Config = toml::from_str(&format!("{base}{section}")).unwrap();
            assert_eq!(
                (
                    cfg.buffer.max_messages,
                    cfg.buffer.flush_interval_ms,
                    cfg.buffer.batch_size,
                ),
                expected,
                "{section}",
            );
        }
    }

    #[test]
    fn buffer_drops_oldest_at_count_limit() {
        let mut buffer = MessageBuffer::new(BufferConfig {
            max_messages: 2,
            ..BufferConfig::default()
        });
        for value in ["one", "two", "three"] {
            buffer.push(buffered_message(value));
        }
        assert_eq!(buffer.stats.dropped, 1);
        assert_eq!(buffer.stats.received, 3);
        let batch = buffer.take_batch(10);
        assert_eq!(
            batch
                .iter()
                .map(|m| m.payload.as_slice())
                .collect::<Vec<_>>(),
            [b"two".as_slice(), b"three".as_slice()]
        );
    }

    #[test]
    fn large_messages_only_evict_at_count_limit() {
        let mut buffer = MessageBuffer::new(BufferConfig {
            max_messages: 2,
            ..BufferConfig::default()
        });
        buffer.push(buffered_message("old"));
        buffer.push(Message {
            topic: "large".into(),
            payload: vec![b'x'; 64 * 1024 * 1024],
        });
        buffer.push(buffered_message("latest"));
        let batch = buffer.take_batch(10);
        assert_eq!(buffer.stats.dropped, 1);
        assert_eq!(batch.len(), 2);
        assert_eq!(batch[0].payload.len(), 64 * 1024 * 1024);
        assert_eq!(batch[1].payload, b"latest");
    }

    #[test]
    fn pipeline_chunks_use_configured_count_without_byte_limit() {
        let limits: BufferConfig = toml::from_str("batch_size = 1500").unwrap();
        let mut buffer = MessageBuffer::new(limits);
        for _ in 0..1_501 {
            buffer.push(buffered_message("x"));
        }
        assert_eq!(buffer.take_batch(2_000).len(), 1_500);
        assert_eq!(buffer.take_batch(2_000).len(), 1);
        for _ in 0..3 {
            buffer.push(buffered_message(&"x".repeat(600_000)));
        }
        assert_eq!(buffer.take_batch(10).len(), 3);
    }

    #[test]
    fn topic_slash_to_colon_key() {
        // key = 消息 topic 的 '/' 替换为 ':'，任意层级/任意前缀
        let k = "demo/tenant-a/device/DB-1/data".replace('/', ":");
        assert_eq!(k, "demo:tenant-a:device:DB-1:data");
        let k = "a/b/c".replace('/', ":");
        assert_eq!(k, "a:b:c");
        let k = "single".replace('/', ":");
        assert_eq!(k, "single");
    }

    #[test]
    fn property_values_preserve_json_semantics() {
        for (input, expected) in [
            ("25.6", Some("25.6")),
            ("1", Some("1")),
            ("-0", Some("-0")),
            ("1e400", Some("1e400")),
            (r#""mode-a""#, Some("mode-a")),
            ("true", Some("true")),
            ("null", None),
            ("[1.2300, false]", Some("[1.2300, false]")),
            (
                r#"{"n":18446744073709551617}"#,
                Some(r#"{"n":18446744073709551617}"#),
            ),
        ] {
            let prop: Property =
                serde_json::from_str(&format!(r#"{{"code":"value","value":{input}}}"#)).unwrap();
            assert_eq!(prop.value.as_deref(), expected, "{input}");
        }
    }

    #[test]
    fn malformed_property_values_are_rejected() {
        for input in [r#""\uD800""#, "01", "NaN", "[1,]"] {
            assert!(
                serde_json::from_str::<DeviceData>(&format!(
                    r#"{{"time":"now","properties":[{{"code":"bad","value":{input}}}]}}"#
                ))
                .is_err(),
                "{input}"
            );
        }
    }

    #[test]
    fn separate_host_and_port_config_is_accepted() {
        let mut cfg: Config = toml::from_str(
            r#"
[mqtt]
host = "broker.example.com"
port = 1883
client_id = "test"
username = ""
password = ""
topic = "a/#"
qos = 1
[redis]
host = "::1"
port = 6379
db = 2
password = "example#/?@%23value"
"#,
        )
        .unwrap();
        assert_eq!(cfg.mqtt.host, "broker.example.com");
        assert_eq!(cfg.mqtt.port, 1883);
        for host in ["localhost", "127.0.0.1", "::1", "2001:db8::1"] {
            cfg.redis.host = host.into();
            for password in ["example#/?@%23value", "中文:p%40ss", ""] {
                cfg.redis.password = password.into();
                let client = redis_client(&cfg.redis).unwrap();
                let info = client.get_connection_info();
                assert_eq!(info.addr(), &redis::ConnectionAddr::Tcp(host.into(), 6379));
                assert_eq!(info.redis_settings().db(), 2);
                assert_eq!(
                    info.redis_settings().password(),
                    if password.is_empty() {
                        None
                    } else {
                        Some(password)
                    }
                );
                assert_eq!(info.redis_settings().username(), None);
            }
        }
        let redis: RedisConfig = toml::from_str("host = 'localhost'\nport = 6379\ndb = 0").unwrap();
        let client = redis_client(&redis).unwrap();
        assert_eq!(
            client.get_connection_info().redis_settings().password(),
            None
        );
        assert_eq!(client.get_connection_info().redis_settings().db(), 0);
    }
}
