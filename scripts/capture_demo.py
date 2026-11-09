"""以固定範例資料擷取文件截圖，不啟動系統取樣或變更語言設定。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.cells import cell_len
from sysmon.app import SystemMonitorApp


GIB = 1024**3
MIB = 1024**2
CORE_LOADS = [
    12, 8, 5, 15, 24, 7, 4, 10, 95, 89, 82, 76,
    18, 11, 7, 3, 9, 14, 22, 8, 6, 17, 13, 5,
    10, 6, 4, 12, 20, 5, 3, 8, 91, 84, 79, 72,
    15, 9, 5, 2, 7, 12, 18, 6, 4, 14, 11, 3,
]


def cpu_times(usage):
    """Each sample accounts for busy time, I/O wait and idle time."""
    return {
        "user": usage * 0.8, "system": usage * 0.18,
        "idle": 99.5 - usage, "iowait": 0.5, "irq": 0.0,
        "softirq": usage * 0.02, "nice": 0.0, "steal": 0.0,
        "guest": 0.0, "guest_nice": 0.0,
    }


def demo_snapshot():
    """All values below are invented examples, independent of the host."""
    usage = sum(CORE_LOADS) / len(CORE_LOADS)
    processes = [
        (4102, "python", sum(CORE_LOADS) - 359, 12 * GIB, "running", 32, "python train.py --config demo.toml"),
        (4120, "ffmpeg", 180, 2 * GIB, "running", 8, "ffmpeg -i input.mp4 output.webm"),
        (4201, "node", 88, 768 * MIB, "running", 12, "node server.js"),
        (4300, "postgres", 42, 1536 * MIB, "sleeping", 6, "postgres -D /data/postgres"),
        (4407, "browser", 24, 2 * GIB, "sleeping", 24, "browser"),
        (4501, "code", 12, 1024 * MIB, "sleeping", 18, "code demo-project"),
        (4603, "rsync", 7, 64 * MIB, "running", 1, "rsync -a assets/ /data/assets/"),
        (4702, "vital", 2, 48 * MIB, "running", 2, "vital"),
        (4800, "terminal", 2, 96 * MIB, "sleeping", 4, "terminal"),
        (4901, "shell", 1, 12 * MIB, "sleeping", 1, "bash"),
        (4902, "ssh-agent", 1, 8 * MIB, "sleeping", 1, "ssh-agent"),
        (4903, "systemd", 0, 16 * MIB, "sleeping", 1, "systemd --user"),
    ]
    snapshot = {
        "timestamp": datetime(2026, 1, 1, 14, 32, 8, tzinfo=timezone.utc).timestamp(),
        "hostname": "workstation", "uptime": 3 * 86400 + 9 * 3600 + 18 * 60,
        "cpu": {
            "percent": usage, "core_ids": list(range(48)), "per_core": CORE_LOADS.copy(),
            "times": cpu_times(usage), "per_core_times": [cpu_times(value) for value in CORE_LOADS],
            "logical": 48, "physical": 24, "frequency_mhz": 3600.0,
            "load": [9.24, 8.86, 7.42], "temperature": 61.0,
            "power_watts": 118.0, "power_status": "",
        },
        "memory": {
            "total": 128 * GIB, "used": 40 * GIB, "available": 88 * GIB,
            "free": 56 * GIB, "cached": 30 * GIB, "buffers": 2 * GIB,
            "shared": 256 * MIB, "percent": 31.25,
            "swap": {"total": 8 * GIB, "used": 512 * MIB, "free": int(7.5 * GIB), "percent": 6.25},
        },
        "processes": [
            {"pid": pid, "name": name, "user": "demo", "cpu": cpu,
             "memory": memory, "status": status, "threads": threads, "command": command}
            for pid, name, cpu, memory, status, threads, command in processes
        ],
        "disks": [
            {"device": "/dev/nvme0n1p2", "mountpoint": "/", "fstype": "ext4",
             "total": 1024 * GIB, "used": 384 * GIB, "free": 640 * GIB, "percent": 37.5},
            {"device": "/dev/nvme1n1p1", "mountpoint": "/data", "fstype": "ext4",
             "total": 2048 * GIB, "used": 1280 * GIB, "free": 768 * GIB, "percent": 62.5},
        ],
        "disk_io": [
            {"name": "nvme0n1", "read_rate": 12 * MIB, "write_rate": 3 * MIB,
             "read_iops": 256, "write_iops": 48, "busy_percent": 7.2},
            {"name": "nvme1n1", "read_rate": 164 * MIB, "write_rate": 28 * MIB,
             "read_iops": 1420, "write_iops": 326, "busy_percent": 28.4},
        ],
        "network": [
            {"name": "eth0", "up": True, "addresses": ["192.0.2.10"],
             "recv_rate": int(12.4 * MIB), "send_rate": int(2.1 * MIB),
             "received": 54 * GIB, "sent": 9 * GIB},
        ],
        "gpus": [{
            "index": 0, "name": "NVIDIA Demo GPU", "uuid": "GPU-demo",
            "utilization": 76.0, "memory_used": 18 * GIB, "memory_total": 24 * GIB,
            "temperature": 64.0, "power_watts": 216.0, "power_limit_watts": 300.0,
            "fan_percent": 48.0,
            "processes": [
                {"pid": 4102, "name": "python", "memory": 16 * GIB, "type": "C", "gpu_utilization": 72},
                {"pid": 4120, "name": "ffmpeg", "memory": 1792 * MIB, "type": "C", "gpu_utilization": 4},
                {"pid": 4407, "name": "browser", "memory": 256 * MIB, "type": "G", "gpu_utilization": 0},
            ],
        }],
        "gpu_status": "",
        "hardware": {
            "os": "Linux", "kernel": "6.x", "architecture": "x86_64",
            "cpu_model": "24-Core Workstation CPU", "sockets": 1,
            "cores_per_socket": 24, "threads_per_core": 2, "virtualization": "Enabled",
            "cache_l1d": "768 KiB", "cache_l1i": "768 KiB", "cache_l2": "24 MiB",
            "cache_l3": "96 MiB", "driver_version": "Demo driver",
            "topology": [{"cpu": cpu, "core": cpu % 24, "socket": 0, "node": 0} for cpu in range(48)],
        },
    }
    by_pid = {p["pid"]: p for p in snapshot["processes"]}
    for gpu in snapshot["gpus"]:
        for process in gpu["processes"]:
            host = by_pid[process["pid"]]
            process.update({"user": host["user"], "device": gpu["index"], "cpu": host["cpu"],
                            "host_memory": host["memory"], "command": host["command"],
                            "memory_percent": process["memory"] / gpu["memory_total"] * 100})
    return snapshot


class DemoApp(SystemMonitorApp):
    def collect(self):
        """Disable the worker before on_mount can start a real Collector."""


def save_svg(app, language, page="overview"):
    path = app.save_screenshot(f"{page}-{language}.svg", ROOT / "docs/images")
    tree = ET.parse(path)
    for element in tree.iter():
        if element.tag.endswith("text") and "textLength" in element.attrib:
            text = "".join(element.itertext())
            if text:
                element.set("textLength", str(float(element.get("textLength")) * cell_len(text) / len(text)))
                element.set("lengthAdjust", "spacingAndGlyphs")
        if element.tag.endswith("style") and element.text:
            element.text = element.text.replace(
                "font-family: Fira Code, monospace;",
                'font-family: "DejaVu Sans Mono", "Noto Sans Mono CJK TC", monospace;',
            )
    tree.write(path, encoding="unicode")
    path = Path(path)
    path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")
    return path


async def capture():
    (ROOT / "docs/images").mkdir(parents=True, exist_ok=True)
    for language in ("zh-TW", "en"):
        app = DemoApp(language=language)
        async with app.run_test(size=(200, 44)) as pilot:
            data = demo_snapshot()
            for index in range(72):
                app.history["cpu"].append(data["cpu"]["percent"] + 11 * math.sin(index / 6) + 4 * math.sin(index / 2))
                app.history["gpu"].append(64 + 12 * math.sin(index / 8) + 5 * math.sin(index / 3))
                app.history["memory"].append(29 + index / 32)
            app.accept_snapshot(data)
            await pilot.pause(0.5)
            assert not app.workers, "Documentation capture must not start a sampling worker"
            assert app.snapshot["hostname"] == "workstation"
            assert all(process["user"] == "demo" for process in app.snapshot["processes"])
            print(save_svg(app, language))
            await pilot.press("4")
            await pilot.pause()
            print(save_svg(app, language, "gpu"))


if __name__ == "__main__":
    os.environ.pop("NO_COLOR", None)
    os.environ["TZ"] = "UTC"
    time.tzset()
    asyncio.run(capture())
