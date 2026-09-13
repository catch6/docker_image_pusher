"""Black-box regression tests; run after `cargo build --locked` (stdlib only)."""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest


BINARY = Path(os.environ.get("MQTT_TO_REDIS_BIN", Path(__file__).resolve().parents[1] / "target/debug/mqtt-to-redis"))


def read_exact(stream, length):
    data = stream.read(length)
    if len(data) != length:
        raise EOFError("connection closed")
    return data


def read_packet(stream):
    header = read_exact(stream, 1)[0]
    length, shift = 0, 0
    while True:
        byte = read_exact(stream, 1)[0]
        length |= (byte & 127) << shift
        shift += 7
        if byte < 128:
            return header, read_exact(stream, length)


def packet(header, payload):
    length = len(payload)
    result = bytearray([header])
    while True:
        byte = length % 128
        length //= 128
        result.append(byte | (128 if length else 0))
        if not length:
            return bytes(result) + payload


class BridgeTests(unittest.TestCase):
    def run_bridge(self, payload, password="", rejected=False, qos=1,
                   buffer_limits=None, expected_count=None, reply_batch=1,
                   stop_before_flush=False, run_seconds=0, late_payloads=(),
                   redis_error=False, first_write_window=(0.25, 0.9),
                   write_windows=None):
        payloads = [payload] if isinstance(payload, str) else payload
        if expected_count is None:
            expected_count = len(payloads) + len(late_payloads)
        commands, errors = [], []
        sent = threading.Event()
        first_write = threading.Event()
        mqtt_progress = threading.Event()
        written = threading.Event()
        stopped = threading.Event()
        sockets, workers = [], []
        timing = {}
        write_times = []

        def serve(handler):
            listener = socket.socket()
            sockets.append(listener)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(4)

            def worker():
                try:
                    conn, _ = listener.accept()
                    sockets.append(conn)
                    conn.settimeout(15)
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    with conn, conn.makefile("rb") as stream:
                        handler(conn, stream)
                except EOFError:
                    pass
                except OSError as error:
                    if not stopped.is_set():
                        errors.append(error)
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=worker, daemon=True)
            workers.append(thread)
            thread.start()
            return listener.getsockname()[1]

        def redis(conn, stream):
            count, pending = 0, 0
            while True:
                line = stream.readline()
                if not line:
                    return
                self.assertTrue(line.startswith(b"*"))
                args = []
                for _ in range(int(line[1:])):
                    size = int(stream.readline()[1:])
                    args.append(read_exact(stream, size).decode())
                    self.assertEqual(read_exact(stream, 2), b"\r\n")
                commands.append(args)
                if args[0] == "HSET":
                    write_times.append(time.monotonic())
                    count += 1
                    pending += 1
                    if count == 1:
                        timing["first_write"] = time.monotonic()
                        first_write.set()
                        if late_payloads:
                            self.assertTrue(mqtt_progress.wait(0.3), "MQTT stalled while Redis reply was pending")
                    if pending == reply_batch or count == expected_count:
                        conn.sendall((b"-ERR test failure\r\n" if redis_error else b":1\r\n") * pending)
                        pending = 0
                    if count == expected_count:
                        written.set()
                else:
                    conn.sendall(b"+OK\r\n")

        def mqtt(conn, stream):
            self.assertEqual(read_packet(stream)[0], 0x10)
            conn.sendall(b"\x20\x02\x00\x00")
            header, body = read_packet(stream)
            self.assertEqual(header, 0x82)
            conn.sendall(packet(0x90, body[:2] + bytes([0x80 if rejected else qos])))
            if not rejected:
                topic = b"a/device/data"
                def publish(items, start):
                    packets = []
                    for i, value in enumerate(items, start):
                        body = len(topic).to_bytes(2, "big") + topic
                        if qos:
                            body += i.to_bytes(2, "big")
                        packets.append(packet(0x32 if qos else 0x30, body + value.encode()))
                    conn.sendall(b"".join(packets))
                timing["sent"] = time.monotonic()
                publish(payloads, 1)
                sent.set()
                if late_payloads:
                    self.assertTrue(first_write.wait(3))
                    publish(late_payloads, len(payloads) + 1)
                while not stopped.is_set():
                    header, body = read_packet(stream)
                    if header == 0x40 and int.from_bytes(body, "big") == len(payloads) + len(late_payloads):
                        mqtt_progress.set()
                    elif header == 0xC0:
                        conn.sendall(b"\xD0\x00")
            else:
                stopped.wait(4)

        redis_port, mqtt_port = serve(redis), serve(mqtt)
        try:
            with tempfile.TemporaryDirectory(prefix="mqtt-redis-test-") as directory:
                Path(directory, "config.toml").write_text(f'''[mqtt]
host = "127.0.0.1"
port = {mqtt_port}
client_id = "regression-test"
username = ""
password = ""
topic = "a/+/data"
qos = {qos}
[redis]
host = "127.0.0.1"
port = {redis_port}
password = {json.dumps(password)}
db = 2
''')
                if buffer_limits is not None:
                    with Path(directory, "config.toml").open("a") as config:
                        config.write("\n[buffer]\n" + "\n".join(f"{key} = {value}" for key, value in buffer_limits.items()))
                proc = subprocess.Popen([str(BINARY)], cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    if rejected:
                        proc.wait(timeout=3)
                    elif stop_before_flush:
                        self.assertTrue(sent.wait(2))
                        time.sleep(0.1)
                        self.assertFalse(first_write.is_set(), "wrote before the flush timer")
                    else:
                        self.assertTrue(written.wait(5), "no HSET received")
                        self.assertIsNone(proc.poll())
                        if run_seconds:
                            time.sleep(max(0, run_seconds - (time.monotonic() - timing["sent"])))
                finally:
                    stopped.set()
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        output, _ = proc.communicate(timeout=7)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        output, _ = proc.communicate()
                if not rejected and not stop_before_flush:
                    windows = {0: first_write_window, **(write_windows or {})}
                    for index, (lower, upper) in windows.items():
                        elapsed = write_times[index] - timing["sent"]
                        self.assertGreater(elapsed, lower, f"HSET {index} was too early")
                        self.assertLess(elapsed, upper, f"HSET {index} was too late")
                return commands, output, proc.returncode
        finally:
            stopped.set()
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            for worker in workers:
                worker.join(timeout=5)
            self.assertFalse(errors, errors)

    def test_password_is_sent_verbatim(self):
        for password in ["example#/?@value", "example%23value", ""]:
            with self.subTest(password=password):
                commands, _, _ = self.run_bridge('{"time":"now"}', password=password)
                auth = [cmd for cmd in commands if cmd[0] == "AUTH"]
                self.assertEqual(auth, [["AUTH", password]] if password else [])
                self.assertIn(["SELECT", "2"], commands)

    def test_values_written_without_precision_loss(self):
        payload = r'''{"time":"now","properties":[
            {"code":"decimal","value":1.2300},
            {"code":"exponent","value":1e3},
            {"code":"big","value":18446744073709551617},
            {"code":"precise","value":1.234567890123456789},
            {"code":"text","value":"line\n\"quoted\"中文"},
            {"code":"flag","value":false},
            {"code":"empty","value":null},
            {"code":"missing"}
        ]}'''
        for qos in [0, 1, 2]:
            with self.subTest(qos=qos):
                commands, output, _ = self.run_bridge(payload, qos=qos)
                hset = next(cmd for cmd in commands if cmd[0] == "HSET")
                self.assertEqual(hset[1], "a:device:data")
                self.assertEqual(dict(zip(hset[2::2], hset[3::2])), {
                    "time": "now", "decimal": "1.2300", "exponent": "1e3",
                    "big": "18446744073709551617", "precise": "1.234567890123456789",
                    "text": 'line\n"quoted"中文', "flag": "false",
                })
                self.assertIn("订阅成功", output)

    def test_rejected_subscription_exits_with_error(self):
        _, output, code = self.run_bridge("", rejected=True)
        self.assertNotEqual(code, 0)
        self.assertIn("[ERROR]", output)
        self.assertIn("订阅被拒绝", output)
        self.assertIn("a/+/data", output)
        self.assertNotIn("订阅成功", output)

    def test_pipeline_sends_all_commands_before_waiting_for_replies(self):
        payloads = [json.dumps({"time": str(i)}) for i in range(12)]
        commands, output, code = self.run_bridge(payloads, reply_batch=12)
        self.assertEqual([cmd[3] for cmd in commands if cmd[0] == "HSET"], [str(i) for i in range(12)])
        self.assertIn("写入确认=12", output)
        self.assertNotIn("[OK]", output)
        self.assertEqual(code, 0)

    def test_overflow_keeps_latest_messages(self):
        payloads = [json.dumps({"time": str(i)}) for i in range(5)]
        commands, output, _ = self.run_bridge(payloads, buffer_limits={"max_messages": 2}, expected_count=2)
        self.assertEqual([cmd[3] for cmd in commands if cmd[0] == "HSET"], ["3", "4"])
        self.assertIn("丢弃=3", output)

    def test_one_mib_mqtt_message_is_forwarded(self):
        # Remaining Length 包含 topic 长度前缀、topic、QoS1 包标识符和 JSON payload。
        overhead = 2 + len("a/device/data") + 2 + len(json.dumps({"time": ""}))
        value = "x" * (1024 * 1024 - overhead)
        commands, _, code = self.run_bridge(json.dumps({"time": value}))
        self.assertIn(["HSET", "a:device:data", "time", value], commands)
        self.assertEqual(code, 0)

    def test_pipeline_has_no_byte_limit(self):
        payload = json.dumps({"time": "x" * 4000})
        commands, output, code = self.run_bridge([payload] * 300, reply_batch=300)
        self.assertEqual(len([cmd for cmd in commands if cmd[0] == "HSET"]), 300)
        self.assertIn("丢弃=0", output)
        self.assertNotIn("待写字节", output)
        self.assertEqual(code, 0)

    def test_full_batches_write_immediately_but_tail_waits_for_timer(self):
        payloads = [json.dumps({"time": str(i)}) for i in range(7)]
        commands, output, code = self.run_bridge(
            payloads[:2], late_payloads=payloads[2:], reply_batch=2,
            buffer_limits={"batch_size": 2, "flush_interval_ms": 1500},
            first_write_window=(0, 0.7),
            write_windows={5: (0, 0.9), 6: (1.2, 2.0)},
        )
        self.assertEqual([cmd[3] for cmd in commands if cmd[0] == "HSET"], [str(i) for i in range(7)])
        self.assertIn("写入确认=7", output)
        self.assertEqual(code, 0)

    def test_custom_interval_flushes_partial_batch(self):
        commands, _, code = self.run_bridge(
            '{"time":"timer"}', buffer_limits={"flush_interval_ms": 200},
            first_write_window=(0.1, 0.65),
        )
        self.assertIn(["HSET", "a:device:data", "time", "timer"], commands)
        self.assertEqual(code, 0)

    def test_zero_buffer_values_are_rejected(self):
        template = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text().split("[buffer]")[0]
        for field in ["max_messages", "flush_interval_ms", "batch_size"]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                Path(directory, "config.toml").write_text(template + f"[buffer]\n{field} = 0\n")
                proc = subprocess.run([str(BINARY)], cwd=directory, capture_output=True, text=True, timeout=2)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(field, proc.stderr)
                self.assertIn("必须大于 0", proc.stderr)

    def test_shutdown_flushes_before_next_timer(self):
        commands, output, code = self.run_bridge('{"time":"shutdown"}', stop_before_flush=True)
        self.assertIn(["HSET", "a:device:data", "time", "shutdown"], commands)
        self.assertIn("写入确认=1", output)
        self.assertEqual(code, 0)

    def test_mqtt_keeps_receiving_while_redis_reply_is_pending(self):
        commands, output, _ = self.run_bridge(
            '{"time":"first"}', late_payloads=['{"time":"second"}'],
            write_windows={1: (0.8, 1.4)},
        )
        self.assertEqual([cmd[3] for cmd in commands if cmd[0] == "HSET"], ["first", "second"])
        self.assertIn("写入确认=2", output)

    def test_invalid_payload_does_not_break_batch(self):
        commands, output, _ = self.run_bridge(['{"time":"first"}', '{bad', '{"time":"last"}'], expected_count=2, reply_batch=2)
        self.assertEqual(len([cmd for cmd in commands if cmd[0] == "HSET"]), 2)
        self.assertIn("非法=1", output)

    def test_failed_batch_is_reported_without_retry(self):
        commands, output, _ = self.run_bridge(['{"time":"a"}', '{"time":"b"}'], reply_batch=2, redis_error=True)
        self.assertEqual(len([cmd for cmd in commands if cmd[0] == "HSET"]), 2)
        self.assertIn("写入未确认=2", output)
        self.assertIn("写入确认=0", output)

    def test_periodic_summary_after_ten_seconds(self):
        _, output, _ = self.run_bridge('{"time":"now"}', run_seconds=10.3)
        self.assertEqual(output.count("[Stats]"), 2)  # 10 秒汇总 + 退出汇总
        self.assertNotIn("[OK]", output)


class ConfigTests(unittest.TestCase):
    def assert_config_error(self, section, original, replacement, expected, invalid_buffer=False):
        template = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
        before, marker, body = template.partition(f"[{section}]")
        self.assertIn(original, body)
        config = before + marker + body.replace(original, replacement, 1)
        if invalid_buffer:
            config = config.replace("max_messages = 1000000", "max_messages = 0")
        with tempfile.TemporaryDirectory(prefix="mqtt-config-test-") as directory:
            Path(directory, "config.toml").write_text(config)
            proc = subprocess.run([str(BINARY)], cwd=directory, capture_output=True, text=True, timeout=2)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("[Config]", proc.stderr)
        self.assertIn(expected, proc.stderr)

    def test_valid_hosts_and_ports_reach_buffer_validation_without_network(self):
        for section, host, port in [("mqtt", "broker.example.com", 1883), ("redis", "redis.example.com", 6379)]:
            for valid in ["localhost", "broker-1.example.com", "redis_service", "broker.example.com.",
                          "127.0.0.1", "::1", "2001:db8::1", "::ffff:192.0.2.1"]:
                with self.subTest(section=section, host=valid):
                    self.assert_config_error(section, f'host = "{host}"', f"host = {json.dumps(valid)}",
                                             "buffer.max_messages", invalid_buffer=True)
            for valid in [1, 65535]:
                with self.subTest(section=section, port=valid):
                    self.assert_config_error(section, f"port = {port}", f"port = {valid}",
                                             "buffer.max_messages", invalid_buffer=True)

    def test_zero_port_is_rejected(self):
        for section, port in [("mqtt", 1883), ("redis", 6379)]:
            with self.subTest(section=section):
                self.assert_config_error(section, f"port = {port}", "port = 0", f"{section}.port")

    def test_invalid_hosts_and_ports_are_rejected(self):
        for section, host, port in [("mqtt", "broker.example.com", 1883), ("redis", "redis.example.com", 6379)]:
            for invalid in ["", " ", " localhost", "localhost ", "bad host", "tcp://localhost",
                            "tls://localhost", "ssl://localhost", "redis://localhost", "localhost/path",
                            "localhost:1883", "127.0.0.1:6379", "[::1]", "[::1]:6379",
                            "not:ipv6", "host?query", "host#fragment", "user@host", "host\\path",
                            "-host", "host-", "host..name"]:
                with self.subTest(section=section, host=invalid):
                    self.assert_config_error(section, f'host = "{host}"', f"host = {json.dumps(invalid)}", f"{section}.host")
            for invalid in ["-1", "65536", '"1883"', '"invalid"']:
                with self.subTest(section=section, port=invalid):
                    self.assert_config_error(section, f"port = {port}", f"port = {invalid}", "port")

    def test_host_and_port_are_required_even_with_legacy_address(self):
        for section, host, port, legacy in [("mqtt", "broker.example.com", 1883, "broker"),
                                          ("redis", "redis.example.com", 6379, "addr")]:
            for field, value in [("host", f'"{host}"'), ("port", str(port))]:
                with self.subTest(section=section, field=field):
                    self.assert_config_error(section, f"{field} = {value}", f'{legacy} = "localhost:{port}"',
                                             f"missing field `{field}`")


if __name__ == "__main__":
    unittest.main()
