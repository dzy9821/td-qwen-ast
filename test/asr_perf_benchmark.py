import asyncio
import json
import time
import argparse
import wave
import struct
import math
import os

try:
    import websockets
except ImportError:
    print("Error: 'websockets' is not installed. Please install it using 'pip install websockets'.")
    exit(1)

class SegmentMetrics:
    def __init__(self, bg_ms, ed_ms, text, recv_time):
        self.bg_ms = bg_ms
        self.ed_ms = ed_ms
        self.text = text
        self.recv_time = recv_time # relative to connection start
        self.asr_ms = 0
        self.total_ms = 0
        self.segment_e2e_ms = 0

class ConnectionMetrics:
    def __init__(self, conn_id):
        self.conn_id = conn_id
        self.segments = []
        self.bottleneck_count = 0
        self.success = False
        self.error = None
        self.bottleneck_details = []

class ResourceSample:
    def __init__(self, timestamp, cpu_percent, mem_percent, mem_used_mb):
        self.timestamp = timestamp
        self.cpu_percent = cpu_percent
        self.mem_percent = mem_percent
        self.mem_used_mb = mem_used_mb

class LevelResult:
    def __init__(self, level):
        self.level = level
        self.connections = []
        self.resource_samples = []
        self.wall_time = 0.0

def load_wav_pcm(path, chunk_samples=640):
    with wave.open(path, 'rb') as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 16000:
            raise ValueError("WAV must be 16kHz, 16-bit, mono")
        frames = wf.readframes(wf.getnframes())
    
    samples = struct.unpack(f'<{len(frames)//2}h', frames)
    chunks = []
    for i in range(0, len(samples), chunk_samples):
        chunk = samples[i:i+chunk_samples]
        if len(chunk) < chunk_samples:
            chunk = chunk + (0,) * (chunk_samples - len(chunk))
        chunks.append(struct.pack(f'<{len(chunk)}h', *chunk))
    return chunks

class SystemMonitor:
    def __init__(self):
        self.last_cpu_times = self._get_cpu_times()
        self.last_time = time.time()

    def _get_cpu_times(self):
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline()
                parts = line.split()
                if parts[0] == 'cpu':
                    return sum(float(x) for x in parts[1:8]), float(parts[4]) # total, idle
        except:
            pass
        return 0.0, 0.0

    def _get_mem_info(self):
        mem_total = 0
        mem_available = 0
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        mem_total = int(line.split()[1])
                    elif line.startswith('MemAvailable:'):
                        mem_available = int(line.split()[1])
        except:
            pass
        if mem_total == 0:
            return 0.0, 0.0
        
        used = mem_total - mem_available
        mem_percent = (used / mem_total) * 100.0
        mem_used_mb = used / 1024.0
        return mem_percent, mem_used_mb

    def sample(self):
        now = time.time()
        cpu_times = self._get_cpu_times()
        
        cpu_percent = 0.0
        if self.last_cpu_times[0] > 0 and cpu_times[0] > self.last_cpu_times[0]:
            total_diff = cpu_times[0] - self.last_cpu_times[0]
            idle_diff = cpu_times[1] - self.last_cpu_times[1]
            if total_diff > 0:
                cpu_percent = 100.0 * (total_diff - idle_diff) / total_diff

        self.last_cpu_times = cpu_times
        self.last_time = now

        mem_percent, mem_used_mb = self._get_mem_info()
        return ResourceSample(now, cpu_percent, mem_percent, mem_used_mb)

async def _resource_monitor(level_result, stop_event, interval=1.0):
    monitor = SystemMonitor()
    # Initial sample to set baseline for CPU
    monitor.sample()
    await asyncio.sleep(0.1)
    
    while not stop_event.is_set():
        sample = monitor.sample()
        level_result.resource_samples.append(sample)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

async def _run_connection(conn_id, url, chunks, chunk_samples, args, start_delay):
    metrics = ConnectionMetrics(conn_id)
    if start_delay > 0:
        await asyncio.sleep(start_delay)

    conn_start_time = time.monotonic()
    
    try:
        async with websockets.connect(url, open_timeout=args.open_timeout) as ws:
            
            async def send_audio():
                t_base = time.monotonic()
                chunk_duration = chunk_samples / 16000.0
                for i, chunk in enumerate(chunks):
                    target_time = t_base + i * chunk_duration
                    now = time.monotonic()
                    if now < target_time:
                        await asyncio.sleep(target_time - now)
                    
                    req = {
                        "header": {
                            "action": "run",
                            "task_id": f"task_{conn_id}_{i}"
                        },
                        "payload": {
                            "audio": chunk.hex(),
                            "is_speaking": True,
                            "encoding": "pcm"
                        }
                    }
                    await ws.send(json.dumps(req))
                
                # Send EOS
                req = {
                    "header": {
                        "action": "run",
                        "task_id": f"task_{conn_id}_eos"
                    },
                    "payload": {
                        "audio": "",
                        "is_speaking": False,
                        "encoding": "pcm"
                    }
                }
                await ws.send(json.dumps(req))
            
            async def recv_results():
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=args.recv_timeout)
                        resp = json.loads(msg)
                        payload = resp.get("payload", {})
                        
                        if payload.get("is_final", False):
                            recv_wall_ms = (time.monotonic() - conn_start_time) * 1000.0
                            bg_ms = payload.get("bg", 0)
                            ed_ms = payload.get("ed", 0)
                            text = payload.get("text", "")
                            
                            seg = SegmentMetrics(bg_ms, ed_ms, text, recv_wall_ms)
                            seg.segment_e2e_ms = recv_wall_ms - ed_ms
                            metrics.segments.append(seg)
                        
                        # Stop on status=2
                        if resp.get("header", {}).get("status") == 2:
                            break
                            
                    except asyncio.TimeoutError:
                        break
            
            await asyncio.gather(send_audio(), recv_results())
            metrics.success = True
            
    except Exception as e:
        metrics.error = str(e)
    
    return metrics

def _detect_bottlenecks(metrics):
    metrics.segments.sort(key=lambda x: x.bg_ms)
    for i in range(len(metrics.segments) - 1):
        seg_n = metrics.segments[i]
        seg_n1 = metrics.segments[i + 1]
        
        recv_wall_ms = seg_n.recv_time
        next_seg_ed_ms = seg_n1.ed_ms
        
        if recv_wall_ms > next_seg_ed_ms:
            metrics.bottleneck_count += 1
            metrics.bottleneck_details.append({
                "seg_idx": i,
                "recv_time": recv_wall_ms,
                "next_ed": next_seg_ed_ms,
                "diff": recv_wall_ms - next_seg_ed_ms
            })

async def run_level(level, url, chunks, chunk_samples, args):
    print(f"\n--- Running Level: {level} Connections ---")
    result = LevelResult(level)
    stop_event = asyncio.Event()
    
    monitor_task = asyncio.create_task(_resource_monitor(result, stop_event))
    
    tasks = []
    level_start = time.monotonic()
    
    for i in range(level):
        delay = i * (args.stagger_ms / 1000.0)
        task = asyncio.create_task(_run_connection(i, url, chunks, chunk_samples, args, delay))
        tasks.append(task)
    
    results = await asyncio.gather(*tasks)
    result.wall_time = time.monotonic() - level_start
    
    stop_event.set()
    await monitor_task
    
    for m in results:
        _detect_bottlenecks(m)
        result.connections.append(m)
        
    return result

def percentile(data, p):
    if not data:
        return 0.0
    s_data = sorted(data)
    idx = int(math.ceil(p / 100.0 * len(s_data))) - 1
    idx = max(0, min(idx, len(s_data) - 1))
    return s_data[idx]

def generate_report(results, args, audio_duration_ms):
    lines = []
    lines.append("====================================================================")
    lines.append("ASR 端到端性能基准测试报告")
    lines.append("====================================================================")
    lines.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"服务地址: {args.url}")
    lines.append(f"音频信息: {args.audio} ({audio_duration_ms/1000.0:.2f}s)")
    lines.append(f"并发级别: {','.join(map(str, args.levels))}")
    lines.append("")
    
    lines.append("一、汇总总览表")
    header = f"{'并发':<6} | {'成功':<5} | {'失败':<5} | {'总耗时(s)':<8} | {'CPU avg':<8} | {'CPU max':<8} | {'MEM avg':<8} | {'MEM max':<8} | {'E2E avg':<8} | {'E2E P99':<8} | {'瓶颈次数':<8} | {'整体RTF':<8}"
    lines.append(header)
    lines.append("-" * len(header))
    
    for res in results:
        success = sum(1 for c in res.connections if c.success)
        fail = len(res.connections) - success
        
        cpu_avg = sum(s.cpu_percent for s in res.resource_samples) / max(1, len(res.resource_samples))
        cpu_max = max((s.cpu_percent for s in res.resource_samples), default=0.0)
        
        mem_avg = sum(s.mem_used_mb for s in res.resource_samples) / max(1, len(res.resource_samples))
        mem_max = max((s.mem_used_mb for s in res.resource_samples), default=0.0)
        
        all_e2e = []
        bottlenecks = 0
        for c in res.connections:
            bottlenecks += c.bottleneck_count
            for s in c.segments:
                all_e2e.append(s.segment_e2e_ms)
        
        e2e_avg = sum(all_e2e) / max(1, len(all_e2e))
        e2e_p99 = percentile(all_e2e, 99)
        
        rtf = res.wall_time / (audio_duration_ms / 1000.0)
        
        row = f"{res.level:<6} | {success:<5} | {fail:<5} | {res.wall_time:<10.2f} | {cpu_avg:<8.1f}% | {cpu_max:<8.1f}% | {mem_avg:<8.0f}M | {mem_max:<8.0f}M | {e2e_avg:<8.0f} | {e2e_p99:<8.0f} | {bottlenecks:<8} | {rtf:<8.2f}"
        lines.append(row)
        
    lines.append("")
    lines.append("二、各并发级别详细报告")
    
    for res in results:
        lines.append(f"\n--- 并发级别 {res.level} ---")
        lines.append("2.1 资源使用曲线（采样间隔 ~1s）")
        lines.append(f"{'时间':<10} | {'CPU (%)':<10} | {'MEM (MB)':<10}")
        for i, s in enumerate(res.resource_samples):
            lines.append(f"{i:<10} | {s.cpu_percent:<10.1f} | {s.mem_used_mb:<10.0f}")
            
        lines.append("\n2.2 E2E 延迟统计")
        all_e2e = []
        for c in res.connections:
            for s in c.segments:
                all_e2e.append(s.segment_e2e_ms)
        
        if all_e2e:
            lines.append(f"  - avg: {sum(all_e2e)/len(all_e2e):.1f} ms")
            lines.append(f"  - p50: {percentile(all_e2e, 50):.1f} ms")
            lines.append(f"  - p99: {percentile(all_e2e, 99):.1f} ms")
            lines.append(f"  - max: {max(all_e2e):.1f} ms")
        else:
            lines.append("  无有效数据")
            
        lines.append("\n2.3 瓶颈检测详情")
        total_bn = sum(c.bottleneck_count for c in res.connections)
        lines.append(f"  - 总计瓶颈次数: {total_bn}")
        for c in res.connections:
            if c.bottleneck_count > 0:
                lines.append(f"  - 连接 {c.conn_id}: 瓶颈 {c.bottleneck_count} 次")
                for d in c.bottleneck_details:
                    lines.append(f"      第 {d['seg_idx']} 段延迟导致瓶颈，超出 {d['diff']:.1f} ms")
                    
        lines.append("\n2.4 分段明细 (仅展示出现问题的连接的前几个分段)")
        for c in res.connections:
            if c.error:
                lines.append(f"  - 连接 {c.conn_id}: 失败 ({c.error})")
            elif c.bottleneck_count > 0:
                lines.append(f"  - 连接 {c.conn_id} 分段:")
                for i, s in enumerate(c.segments[:5]):
                    lines.append(f"      [{s.bg_ms}-{s.ed_ms}] e2e={s.segment_e2e_ms:.1f}ms : {s.text}")
                if len(c.segments) > 5:
                    lines.append("      ...")

    lines.append("\n三、报告结束")
    lines.append("====================================================================")
    
    report_text = "\n".join(lines)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report_text)
    
    print(f"\nReport saved to {args.output}")


async def amain():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://localhost:8856/tuling/ast/v3")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--levels", default="1,8,16,32,64,128,256,512")
    parser.add_argument("--chunk-samples", type=int, default=640)
    parser.add_argument("--open-timeout", type=float, default=5.0)
    parser.add_argument("--recv-timeout", type=float, default=10.0)
    parser.add_argument("--stagger-ms", type=float, default=20.0)
    parser.add_argument("--cooldown", type=float, default=10.0)
    parser.add_argument("--output", default="asr_perf_report.txt")
    
    args = parser.parse_args()
    
    levels = [int(x.strip()) for x in args.levels.split(",") if x.strip()]
    
    print(f"Loading audio {args.audio}...")
    chunks = load_wav_pcm(args.audio, args.chunk_samples)
    audio_duration_ms = len(chunks) * args.chunk_samples / 16.0
    print(f"Audio loaded. Duration: {audio_duration_ms/1000.0:.2f}s, Chunks: {len(chunks)}")
    
    results = []
    
    for i, level in enumerate(levels):
        res = await run_level(level, args.url, chunks, args.chunk_samples, args)
        results.append(res)
        
        if i < len(levels) - 1:
            print(f"Cooldown for {args.cooldown} seconds...")
            await asyncio.sleep(args.cooldown)
            
    generate_report(results, args, audio_duration_ms)

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()
