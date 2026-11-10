"""Profile live, paused, or idle operation in a real PTY at a 1-second interval.

Profile UI and collector in separate runs. cProfile uses wall time; thread CPU
measurements exclude waits. Use --profile none for uninstrumented CPU results.
Python 3.12's cProfile also records other threads in the measurement window.
"""

import argparse
import asyncio
import cProfile
import fcntl
import json
import math
import os
from pathlib import Path
import pstats
import select
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

import psutil


PAGES = ("overview", "cpu", "processes", "gpu", "storage", "hardware")


def save_profile(profile, path):
    profile.dump_stats(str(path.with_suffix(".prof")))
    with path.with_suffix(".txt").open("w") as stream:
        stats = pstats.Stats(profile, stream=stream)
        stats.sort_stats("cumulative").print_stats(60)
        stats.sort_stats("tottime").print_stats(60)


def run_app(args):
    sys.path.insert(0, str(args.root))
    from sysmon.app import SystemMonitorApp
    from sysmon.collector import Collector

    ui_profile = cProfile.Profile()
    collector_profile = cProfile.Profile()
    measuring = threading.Event()
    sample_lock = threading.Lock()
    samples = []
    accepted = 0
    latest_sample = None
    original_sample = Collector.sample
    profile_collector = args.profile == "collector"
    profile_ui = args.profile == "ui"
    result = {}

    def sample(collector):
        nonlocal latest_sample
        with sample_lock:
            if not measuring.is_set():
                latest_sample = original_sample(collector)
                return latest_sample
            cpu_start = time.thread_time()
            wall_start = time.perf_counter()
            if profile_collector:
                collector_profile.enable()
            try:
                latest_sample = original_sample(collector)
                return latest_sample
            finally:
                if profile_collector:
                    collector_profile.disable()
                samples.append({"cpu_ms": 1000 * (time.thread_time() - cpu_start),
                                "wall_ms": 1000 * (time.perf_counter() - wall_start)})

    Collector.sample = sample

    class ProfiledApp(SystemMonitorApp):
        def accept_snapshot(self, data):
            nonlocal accepted
            if measuring.is_set():
                accepted += 1
            super().accept_snapshot(data)

    app = ProfiledApp(interval=1, language="en")

    async def measure(pilot):
        deadline = time.monotonic() + 20
        while app.snapshot is None or app.snapshot["cpu"]["percent"] is None:
            if app.sampling_error:
                raise RuntimeError(app.sampling_error)
            if time.monotonic() > deadline:
                raise TimeoutError("No measured system snapshot arrived")
            await asyncio.sleep(.1)
        await pilot.press(str(PAGES.index(args.page) + 1))
        if args.mode == "paused":
            await pilot.press("space")
        elif args.mode == "idle":
            app.stop_sampling.set()
        # Leave page changes, the initial table population, and animations out.
        await asyncio.sleep(3)
        process = psutil.Process()
        before_cpu = process.cpu_times()
        start_wall = time.perf_counter()
        start_cpu = time.thread_time()
        measuring.set()
        if profile_ui:
            ui_profile.enable()
        try:
            await asyncio.sleep(args.seconds)
        finally:
            if profile_ui:
                ui_profile.disable()
            measuring.clear()
        ui_cpu = time.thread_time() - start_cpu
        elapsed = time.perf_counter() - start_wall
        after_cpu = process.cpu_times()
        result.update({
            "page": args.page, "mode": args.mode, "profile": args.profile,
            "python": sys.version.split()[0],
            "interval_seconds": 1, "terminal": [args.width, args.height],
            "seconds": elapsed, "ui_cpu_seconds": ui_cpu,
            "ui_cpu_percent_one_core": 100 * ui_cpu / elapsed,
            "process_cpu_percent_one_core": 100 * (
                after_cpu.user + after_cpu.system - before_cpu.user - before_cpu.system
            ) / elapsed,
            "rss_mib": process.memory_info().rss / 1024**2,
            "snapshots_accepted": accepted,
        })
        if app.sampling_error:
            raise RuntimeError(app.sampling_error)
        app.action_quit()

    try:
        app.run(auto_pilot=measure)
    finally:
        measuring.clear()
        app.stop_sampling.set()
        # A sample already running at the end must finish before dumping stats.
        with sample_lock:
            pass
        Collector.sample = original_sample
    if not result:
        raise RuntimeError("Profiling did not finish")
    result["collector_samples"] = samples
    result["collector_cpu_seconds"] = sum(s["cpu_ms"] for s in samples) / 1000
    result["logical_cpus"] = latest_sample["cpu"]["logical"]
    result["process_count"] = len(latest_sample["processes"])
    if profile_ui:
        save_profile(ui_profile, args.output / "ui")
    if profile_collector and samples:
        save_profile(collector_profile, args.output / "collector")
    (args.output / "report.json").write_text(json.dumps(result, indent=2) + "\n")


def run_terminal(args):
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".profile-config-", dir=args.output) as config:
        master, slave = os.openpty()
        process = None
        output = bytearray()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", args.height, args.width, 0, 0))
            original_terminal = termios.tcgetattr(slave)
            environment = {**os.environ, "XDG_CONFIG_HOME": config, "TERM": "xterm-256color"}
            for variable in ("COLUMNS", "LINES", "TEXTUAL_ALLOW_SIGNALS"):
                environment.pop(variable, None)
            command = [sys.executable, str(Path(__file__).resolve()), "--child",
                       "--root", str(args.root), "--output", str(args.output),
                       "--page", args.page, "--mode", args.mode, "--profile", args.profile,
                       "--seconds", str(args.seconds), "--width", str(args.width), "--height", str(args.height)]
            process = subprocess.Popen(command, cwd=args.root, env=environment,
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            deadline = time.monotonic() + args.seconds + 60
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("Profiling child did not finish")
                if select.select([master], [], [], .1)[0]:
                    output.extend(os.read(master, 65536))
            while select.select([master], [], [], 0)[0]:
                output.extend(os.read(master, 65536))
            if process.returncode:
                (args.output / "failure-terminal.raw").write_bytes(output)
                raise RuntimeError(f"Profiling child exited {process.returncode}; see failure-terminal.raw")
            if termios.tcgetattr(slave) != original_terminal or b"\x1b[?1049l" not in output:
                raise RuntimeError("Terminal state was not restored")
            report_path = args.output / "report.json"
            result = json.loads(report_path.read_text())
            result["terminal_bytes_total"] = len(output)
            result["terminal_restored"] = True
            report_path.write_text(json.dumps(result, indent=2) + "\n")
            (args.output / "failure-terminal.raw").unlink(missing_ok=True)
            print(json.dumps({key: value for key, value in result.items() if key != "collector_samples"}))
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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--height", type=int, default=44)
    parser.add_argument("--page", choices=PAGES, default="overview")
    parser.add_argument("--mode", choices=("live", "paused", "idle"), default="live")
    parser.add_argument("--profile", choices=("ui", "collector", "none"), default="ui")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0 or args.width <= 0 or args.height <= 0:
        parser.error("seconds must be finite and seconds, width and height must be positive")
    args.root = args.root.resolve()
    args.output = args.output.resolve()
    run_app(args) if args.child else run_terminal(args)


if __name__ == "__main__":
    main()
