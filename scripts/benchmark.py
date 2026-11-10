"""Measure the live sampler and TUI; CPU percentages use one logical core."""

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import select
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import time

import psutil


def drain(master, process, seconds, *, exiting=False):
    deadline = time.monotonic() + seconds
    tail = b""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            if exiting:
                return
            raise RuntimeError(f"TUI exited early: {process.returncode}")
        if select.select([master], [], [], min(.1, max(0, deadline - time.monotonic())))[0]:
            data = tail + os.read(master, 65536)
            if b"Traceback" in data or b"Could not read system data:" in data:
                raise RuntimeError(data.decode(errors="replace"))
            tail = data[-256:]
    if exiting and process.poll() is None:
        raise TimeoutError("TUI did not exit")


def terminal(root, seconds, width, height, page):
    with tempfile.TemporaryDirectory(prefix="vital-benchmark-") as config:
        master, slave = os.openpty()
        process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
            env = {**os.environ, "XDG_CONFIG_HOME": config, "TERM": "xterm-256color"}
            process = subprocess.Popen([sys.executable, "-m", "sysmon", "--lang", "en"],
                                       cwd=root, env=env, stdin=slave, stdout=slave, stderr=slave,
                                       start_new_session=True)
            drain(master, process, 3)
            os.write(master, page.encode())
            drain(master, process, 2)
            monitor = psutil.Process(process.pid)
            before = monitor.cpu_times()
            start = time.monotonic()
            drain(master, process, seconds)
            elapsed = time.monotonic() - start
            after = monitor.cpu_times()
            result = {
                "cpu_percent_one_core": round(100 * (after.user + after.system - before.user - before.system) / elapsed, 2),
                "rss_mib": round(monitor.memory_info().rss / 1024**2, 1),
                "seconds": round(elapsed, 2), "terminal": [width, height],
            }
            os.write(master, b"q")
            drain(master, process, 5, exiting=True)
            if process.returncode:
                raise RuntimeError(f"TUI exit status: {process.returncode}")
            return result
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            os.close(master)
            os.close(slave)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--height", type=int, default=44)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0 or args.width <= 0 or args.height <= 0:
        parser.error("seconds must be finite and seconds, width and height must be positive")
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    from sysmon.collector import Collector

    collector = Collector()
    durations = []
    try:
        collector.sample()
        for _ in range(10):
            time.sleep(.2)
            start = time.process_time()
            snapshot = collector.sample()
            durations.append((time.process_time() - start) * 1000)
    finally:
        collector.close()
    print(json.dumps({"collector_cpu_ms": round(statistics.mean(durations), 2),
                      "process_count": len(snapshot["processes"]), "logical_cpus": snapshot["cpu"]["logical"]}), flush=True)
    for page, name in (("1", "overview"), ("3", "processes")):
        print(json.dumps({name: terminal(root, args.seconds, args.width, args.height, page)}), flush=True)


if __name__ == "__main__":
    main()
