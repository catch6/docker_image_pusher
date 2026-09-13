"""Isolated load test using real local Redis/Mosquitto and the release binary.

Python standard library only. Starts loopback-only services with persistence off.
Usage: python3 tests/load_test.py --output /tmp/mqtt-load --suite full
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import socket
import statistics
import subprocess
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def mqtt_packet(header, body):
    size = len(body)
    result = bytearray([header])
    while True:
        byte = size % 128
        size //= 128
        result.append(byte | (128 if size else 0))
        if not size:
            return bytes(result) + body


def exact(stream, count):
    result = stream.read(count)
    if len(result) != count:
        raise EOFError()
    return result


def mqtt_read(stream):
    header = exact(stream, 1)[0]
    size, shift = 0, 0
    while True:
        byte = exact(stream, 1)[0]
        size |= (byte & 127) << shift
        shift += 7
        if byte < 128:
            return header, exact(stream, size)


def resp(args):
    args = [str(arg).encode() if not isinstance(arg, bytes) else arg for arg in args]
    return b'*%d\r\n' % len(args) + b''.join(b'$%d\r\n' % len(arg) + arg + b'\r\n' for arg in args)


def redis_read(stream):
    line = stream.readline()
    if not line:
        raise EOFError()
    kind, value = line[:1], line[1:-2]
    if kind == b'-':
        raise RuntimeError(value.decode())
    if kind == b':':
        return int(value)
    if kind == b'+':
        return value
    if kind == b'$':
        size = int(value)
        return None if size < 0 else exact(stream, size + 2)[:-2]
    if kind == b'*':
        return [redis_read(stream) for _ in range(int(value))]
    raise ValueError(line)


class Redis:
    def __init__(self, port):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.stream = self.sock.makefile('rb')

    def pipeline(self, commands):
        self.sock.sendall(b''.join(resp(command) for command in commands))
        return [redis_read(self.stream) for _ in commands]

    def command(self, *args):
        return self.pipeline([args])[0]

    def close(self):
        self.stream.close()
        self.sock.close()


class ReplyDelay:
    """Delay each response byte by a fixed time; do not serialize per-command sleeps."""
    def __init__(self, redis_port, delay_ms):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

        async def client(reader, writer):
            upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', redis_port)
            pending = asyncio.Queue(maxsize=1024)

            async def requests():
                while data := await reader.read(65536):
                    upstream_writer.write(data)
                    await upstream_writer.drain()

            async def collect_replies():
                while data := await upstream_reader.read(65536):
                    await pending.put((time.monotonic() + delay_ms / 1000, data))

            async def release_replies():
                while True:
                    due, data = await pending.get()
                    await asyncio.sleep(max(0, due - time.monotonic()))
                    writer.write(data)
                    await writer.drain()

            tasks = [asyncio.create_task(fn()) for fn in (requests, collect_replies, release_replies)]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                writer.close()
                upstream_writer.close()
                await writer.wait_closed()
                await upstream_writer.wait_closed()

        async def start():
            return await asyncio.start_server(client, '127.0.0.1', 0)

        self.server = asyncio.run_coroutine_threadsafe(start(), self.loop).result(5)
        self.port = self.server.sockets[0].getsockname()[1]

    def close(self):
        async def stop():
            self.server.close()
            await self.server.wait_closed()
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError('condition not reached')


def cpu_seconds(value):
    parts = value.split(':')
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def process_sample(pids):
    result = subprocess.check_output(['ps', '-o', 'pid=,rss=,time=', '-p', ','.join(map(str, pids))], text=True)
    return {int(pid): {'rss_mib': int(rss) / 1024, 'cpu_s': cpu_seconds(cpu)}
            for pid, rss, cpu in (line.split() for line in result.splitlines())}


def percentiles(values):
    values = sorted(values)
    if not values:
        return {}
    return {name: round(values[min(len(values) - 1, int((len(values) - 1) * fraction))], 2)
            for name, fraction in [('p50_ms', .5), ('p95_ms', .95), ('p99_ms', .99), ('max_ms', 1)]}


def run_case(case, output):
    name, rate, duration, properties, delay = case
    case_dir = output / name
    case_dir.mkdir()
    processes, files, delay_proxy = [], [], None
    stop = threading.Event()
    observer_stop = threading.Event()
    errors, samples, resources = [], [], []
    pending_samples = queue.Queue()
    counters = {'published': 0, 'pubacks': 0}
    last_device_seq = {}

    def start(args, log_name, cwd):
        log = (case_dir / log_name).open('w')
        files.append(log)
        proc = subprocess.Popen(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        processes.append(proc)
        return proc

    with tempfile.TemporaryDirectory(prefix='mqtt-load-') as directory:
        work = Path(directory)
        redis_port, mqtt_port = free_port(), free_port()
        while mqtt_port == redis_port:
            mqtt_port = free_port()
        (work / 'mosquitto.conf').write_text(f'''listener {mqtt_port} 127.0.0.1
allow_anonymous true
persistence false
max_inflight_messages 1000
max_queued_messages 1000000
max_queued_bytes 134217728
log_type error
''')
        try:
            redis_proc = start([shutil.which('redis-server'), '--bind', '127.0.0.1', '--port', str(redis_port),
                                '--save', '', '--appendonly', 'no', '--dir', directory], 'redis.log', directory)
            mqtt_proc = start([shutil.which('mosquitto'), '-c', str(work / 'mosquitto.conf')], 'mosquitto.log', directory)

            def services_ready():
                try:
                    for port in (redis_port, mqtt_port):
                        socket.create_connection(('127.0.0.1', port), timeout=.1).close()
                    return True
                except OSError:
                    return False
            wait_until(services_ready)
            target_port = redis_port
            if delay:
                delay_proxy = ReplyDelay(redis_port, delay)
                target_port = delay_proxy.port
            probe = Redis(target_port)
            rtts = []
            for _ in range(5):
                before = time.monotonic()
                probe.command('PING')
                rtts.append((time.monotonic() - before) * 1000)
            probe.close()
            (work / 'config.toml').write_text(f'''[mqtt]
host = "127.0.0.1"
port = {mqtt_port}
client_id = "bridge-load-test"
username = ""
password = ""
topic = "bench/#"
qos = 1
[redis]
host = "127.0.0.1"
port = {target_port}
password = ""
db = 0
''')
            bridge = start([str(ROOT / 'target/release/mqtt-to-redis')], 'bridge.log', directory)
            wait_until(lambda: '订阅成功' in (case_dir / 'bridge.log').read_text(), timeout=10)
            control = Redis(redis_port)
            initial_hsets = hset_count(control)
            pids = [bridge.pid, redis_proc.pid, mqtt_proc.pid, os.getpid()]
            resource_start = process_sample(pids)

            def monitor():
                while not stop.wait(.5):
                    try:
                        resources.append({'time': time.monotonic(), 'processes': process_sample(pids)})
                    except Exception as error:
                        if not stop.is_set():
                            errors.append(f'monitor: {error}')
            monitor_thread = threading.Thread(target=monitor, daemon=True)
            monitor_thread.start()

            def observe():
                pending = []
                connection = Redis(redis_port)
                try:
                    while not observer_stop.is_set():
                        while True:
                            try:
                                pending.append(pending_samples.get_nowait())
                            except queue.Empty:
                                break
                        if pending:
                            values = connection.pipeline([('HGET', key, 'time') for key, _ in pending])
                            now = time.time_ns()
                            retained = []
                            for (key, stamp), value in zip(pending, values):
                                if value is None:
                                    retained.append((key, stamp))
                                else:
                                    assert int(value) == stamp
                                    samples.append((now - stamp) / 1_000_000)
                            pending = retained
                        observer_stop.wait(.05)
                except Exception as error:
                    errors.append(f'observer: {error}')
                finally:
                    counters['samples_missing'] = len(pending) + pending_samples.qsize()
                    connection.close()
            observer_thread = threading.Thread(target=observe, daemon=True)
            observer_thread.start()

            publisher = socket.create_connection(('127.0.0.1', mqtt_port), timeout=5)
            publisher.settimeout(10)
            publisher.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            stream = publisher.makefile('rb')
            client_id = b'load-publisher'
            publisher.sendall(mqtt_packet(0x10, b'\x00\x04MQTT\x04\x02\x00\x1e' + len(client_id).to_bytes(2, 'big') + client_id))
            assert mqtt_read(stream) == (0x20, b'\x00\x00')
            ids = queue.Queue()
            for packet_id in range(1, 4097):
                ids.put(packet_id)

            def acknowledge():
                try:
                    while not stop.is_set():
                        header, body = mqtt_read(stream)
                        if header == 0x40:
                            counters['pubacks'] += 1
                            ids.put(int.from_bytes(body, 'big'))
                except (EOFError, OSError) as error:
                    if not stop.is_set():
                        errors.append(f'publisher: {error}')
            ack_thread = threading.Thread(target=acknowledge, daemon=True)
            ack_thread.start()
            tail = ','.join(f'{{"code":"p{i}","value":{i}.1234}}' for i in range(properties - 1))
            start_time = time.monotonic()
            max_payload = 0
            total = int(rate * duration)
            burst = name.startswith('burst')
            while counters['published'] < total:
                if not burst:
                    due = start_time + counters['published'] / rate
                    time.sleep(max(0, due - time.monotonic()))
                count = min(200 if burst else max(1, int(rate / 100)), total - counters['published'])
                packets = []
                for _ in range(count):
                    seq = counters['published'] + 1
                    packet_id = ids.get(timeout=10)
                    stamp = time.time_ns()
                    if seq % 200 == 0:
                        topic = f'bench/sample/{seq}'
                        pending_samples.put((topic.replace('/', ':'), stamp))
                    else:
                        topic = f'bench/device/{seq % 1000}'
                        last_device_seq[topic.replace('/', ':')] = seq
                    encoded_topic = topic.encode()
                    payload = f'{{"time":"{stamp}","properties":[{{"code":"seq","value":{seq}}},{tail}]}}'.encode()
                    max_payload = max(max_payload, len(payload))
                    packets.append(mqtt_packet(0x32, len(encoded_topic).to_bytes(2, 'big') + encoded_topic + packet_id.to_bytes(2, 'big') + payload))
                    counters['published'] += 1
                publisher.sendall(b''.join(packets))
            send_elapsed = time.monotonic() - start_time
            wait_until(lambda: counters['pubacks'] == counters['published'], timeout=10)
            ack_elapsed = time.monotonic() - start_time
            sent_hsets = hset_count(control) - initial_hsets
            resources_end = process_sample(pids)
            # Allow the final one-second batch and any bounded backlog to drain.
            time.sleep(4 if delay < 200 else 7)
            bridge.terminate()
            bridge.wait(timeout=8)
            time.sleep(.1)
            observer_stop.set()
            observer_thread.join(6)
            stop.set()
            publisher.shutdown(socket.SHUT_RDWR)
            publisher.close()
            stream.close()
            monitor_thread.join(2)
            ack_thread.join(2)
            log = (case_dir / 'bridge.log').read_text()
            stats_lines = [line for line in log.splitlines() if '[Stats]' in line]
            assert stats_lines, log
            final = {key: int(value) for key, value in re.findall(r'([\u4e00-\u9fff]+)=(\d+)', stats_lines[-1])}
            actual_hsets = hset_count(control) - initial_hsets
            latest = control.pipeline([('HGET', key, 'seq') for key in last_device_seq])
            mismatches = sum(value is None or int(value) != expected for value, expected in zip(latest, last_device_seq.values()))
            redis_memory = control.command('INFO', 'memory').decode()
            control.close()
            cpu = {label: round((resources_end[pid]['cpu_s'] - resource_start[pid]['cpu_s']) / ack_elapsed * 100, 1)
                   for label, pid in zip(('bridge', 'redis', 'mosquitto', 'generator_and_observer'), pids)}
            rss = {label: round(max([resource_start[pid]['rss_mib']] + [r['processes'].get(pid, {}).get('rss_mib', 0) for r in resources]), 2)
                   for label, pid in zip(('bridge', 'redis', 'mosquitto', 'generator_and_observer'), pids)}
            result = dict(name=name, target_rate=rate, duration_s=duration, properties=properties,
                          max_payload_bytes=max_payload, redis_ping_ms=round(statistics.mean(rtts), 2),
                          published=counters['published'], publisher_pubacks=counters['pubacks'],
                          send_elapsed_s=round(send_elapsed, 3), publisher_ack_elapsed_s=round(ack_elapsed, 3),
                          actual_publish_rate=round(counters['published'] / ack_elapsed, 1),
                          hsets_at_publish_end=sent_hsets, final_hsets=actual_hsets,
                          final_bridge_stats=final, latest_device_mismatches=mismatches,
                          latency_samples=len(samples), missing_samples=counters['samples_missing'],
                          sampled_end_to_end_latency=percentiles(samples), avg_cpu_percent=cpu, peak_rss_mib=rss,
                          bridge_exit_code=bridge.returncode, bridge_errors=log.count('[ERROR]'), errors=errors)
            result['checks'] = {
                'publisher_and_bridge_counts_match': counters['pubacks'] == counters['published'] == final['累计接收'],
                'redis_and_bridge_counts_match': actual_hsets == final['写入确认'],
                'all_received_messages_accounted_for': final['累计接收'] == final['写入确认'] + final['丢弃'],
                'latest_device_values_correct': mismatches == 0,
                'clean_exit': bridge.returncode == 0,
                'measurement_errors_absent': not errors,
            }
            (case_dir / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
            (case_dir / 'resources.json').write_text(json.dumps(resources))
            (case_dir / 'redis-memory.txt').write_text(redis_memory)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result
        finally:
            observer_stop.set()
            stop.set()
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            if delay_proxy:
                delay_proxy.close()
            for log in files:
                log.close()


def hset_count(connection):
    info = connection.command('INFO', 'commandstats').decode()
    match = re.search(r'cmdstat_hset:calls=(\d+)', info)
    return int(match[1]) if match else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--suite', choices=['smoke', 'full'], default='full')
    parser.add_argument('--case', action='append', dest='case_names',
                        help='Only run the named scenario; repeat to select several.')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {'platform': platform.platform(), 'python': platform.python_version(),
                'logical_cpus': os.cpu_count(), 'date': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'main_rs_sha256': hashlib.sha256((ROOT / 'main.rs').read_bytes()).hexdigest(),
                'redis_version': subprocess.check_output([shutil.which('redis-server'), '--version'], text=True).strip()}
    (args.output / 'environment.json').write_text(json.dumps(metadata, indent=2))
    cases = [('smoke', 1000, 3, 10, 0)] if args.suite == 'smoke' else [
        ('steady_1000', 1000, 30, 10, 0),
        ('steady_5000', 5000, 60, 10, 0),
        ('steady_10000', 10000, 30, 10, 0),
        ('steady_20000', 20000, 30, 10, 0),
        ('large_1000', 1000, 30, 100, 0),
        ('redis_100ms', 5000, 30, 10, 100),
        ('redis_300ms', 5000, 30, 10, 300),
        ('burst_50000', 50000, 1, 10, 0),
    ]
    if args.case_names:
        unknown = set(args.case_names) - {case[0] for case in cases}
        if unknown:
            parser.error('unknown scenario(s): ' + ', '.join(sorted(unknown)))
        cases = [case for case in cases if case[0] in args.case_names]
    results = []
    for case in cases:
        print(f'START {case[0]} target={case[1]}/s duration={case[2]}s', flush=True)
        results.append(run_case(case, args.output))
        (args.output / 'results.json').write_text(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
