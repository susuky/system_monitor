"""Run with: python -m unittest discover -s tests -p test_collector.py -v."""

from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sysmon.collector import Collector, CPU_FIELDS, _cpu_usage, _read_cpu_times


class CollectorTests(unittest.TestCase):
    def test_live_counters_and_process_cpu(self):
        collector = Collector()
        child = subprocess.Popen([sys.executable, "-c", "while True: pass"])
        try:
            with ThreadPoolExecutor(max_workers=1) as worker:
                first = worker.submit(collector.sample).result()
            time.sleep(0.2)
            with ThreadPoolExecutor(max_workers=1) as worker:
                second = worker.submit(collector.sample).result()
            json.dumps(second, allow_nan=False)
            self.assertIsNone(first["cpu"]["percent"])
            self.assertTrue(all(value is None for value in first["cpu"]["per_core"]))
            self.assertTrue(0 <= second["cpu"]["percent"] <= 100)
            self.assertEqual(len(second["cpu"]["per_core"]), os.cpu_count())
            self.assertEqual(len(second["cpu"]["core_ids"]), len(second["cpu"]["per_core"]))
            for row in second["cpu"]["per_core_times"]:
                if row["idle"] is not None:
                    self.assertAlmostEqual(sum(row.values()), 100)
            processes = {row["pid"]: row for row in second["processes"]}
            self.assertGreater(processes[child.pid]["cpu"], 0)
            self.assertGreater(processes[child.pid]["memory"], 0)
            memory = second["memory"]
            self.assertEqual(memory["used"] + memory["available"], memory["total"])
            self.assertTrue(any(row["mountpoint"] == "/" for row in second["disks"]))
            self.assertTrue(all(row["recv_rate"] is None for row in first["network"]))
            for gpu in second["gpus"]:
                pids = [row["pid"] for row in gpu["processes"]]
                self.assertEqual(len(pids), len(set(pids)))
        finally:
            child.terminate()
            child.wait(timeout=5)
            collector.close()
            collector.close()

    def test_guest_time_and_iowait_are_not_double_counted(self):
        Times = namedtuple("Times", CPU_FIELDS)
        previous = Times(*([0] * len(CPU_FIELDS)))
        current = Times(user=40, system=20, idle=20, iowait=10, irq=0,
                        softirq=0, nice=10, steal=0, guest=10, guest_nice=5)
        percent, breakdown = _cpu_usage(current, previous)
        self.assertEqual(percent, 70)
        self.assertEqual(breakdown["user"], 30)
        self.assertEqual(breakdown["nice"], 5)
        self.assertEqual(sum(breakdown.values()), 100)
        self.assertIsNone(_cpu_usage(current, current)[0])

    def test_gapped_cpu_ids_and_hotplug_keep_their_own_counters(self):
        collector = Collector()
        try:
            with tempfile.TemporaryDirectory(prefix="sysmon-collector-test-") as directory:
                path = Path(directory) / "stat"
                path.write_text("cpu 90 0 0 110\ncpu0 80 0 0 20\ncpu2 10 0 0 90\nintr 123\n")
                first = _read_cpu_times(path)
                path.write_text("cpu 95 0 0 115\ncpu2 90 0 0 110\ncpu3 5 0 0 5\nintr 456\n")
                second = _read_cpu_times(path)
            with patch("sysmon.collector._read_cpu_times", side_effect=[first, second]):
                initial = collector._cpu()
                updated = collector._cpu()
            self.assertEqual(initial["core_ids"], [0, 2])
            self.assertEqual(initial["per_core"], [None, None])
            self.assertEqual(updated["core_ids"], [2, 3])
            self.assertEqual(updated["per_core"], [80, None])
            self.assertEqual(updated["per_core_times"][0]["user"], 80)
            self.assertIsNone(updated["per_core_times"][1]["user"])
        finally:
            collector.close()

    def test_rapl_wrap_and_multiple_packages(self):
        collector = Collector.__new__(Collector)
        collector._power_previous = None
        with tempfile.TemporaryDirectory(prefix="sysmon-collector-test-") as directory:
            paths = [Path(directory) / name for name in ("package-0", "package-1")]
            paths[0].write_text("95")
            paths[1].write_text("25")
            collector._power_domains = [(path, 100) for path in paths]
            with patch("sysmon.collector.time.monotonic", side_effect=[5, 7]):
                self.assertIsNone(collector._cpu_power()[0])
                paths[0].write_text("5")
                paths[1].write_text("65")
                power, status = collector._cpu_power()
            self.assertAlmostEqual(power, 25e-6)
            self.assertEqual(status, "")

    def test_gpu_utilization_requires_recent_valid_samples(self):
        collector = Collector.__new__(Collector)
        now = 10_000_000
        samples = [
            SimpleNamespace(pid=1, timeStamp=now - 800_000, smUtil=20),
            SimpleNamespace(pid=1, timeStamp=now - 200_000, smUtil=37),
            SimpleNamespace(pid=2, timeStamp=now - 200_000, smUtil=0),
            SimpleNamespace(pid=3, timeStamp=now - 2_000_001, smUtil=80),
            SimpleNamespace(pid=4, timeStamp=now - 200_000, smUtil=101),
            SimpleNamespace(pid=5, timeStamp=now + 1, smUtil=40),
            SimpleNamespace(pid=99, timeStamp=now - 200_000, smUtil=25),
        ]
        collector._nvml = SimpleNamespace(
            NVMLError=RuntimeError, nvmlDeviceGetProcessUtilization=lambda handle, since: samples,
        )
        with patch("sysmon.collector.time.time_ns", return_value=now * 1000):
            self.assertEqual(collector._gpu_process_utilization("gpu", {1, 2, 3, 4, 5}), {1: 37.0, 2: 0.0})
            with patch.object(collector._nvml, "nvmlDeviceGetProcessUtilization", return_value=[]):
                self.assertEqual(collector._gpu_process_utilization("gpu", {1}), {})
            with patch.object(collector._nvml, "nvmlDeviceGetProcessUtilization", side_effect=RuntimeError("Not supported")):
                self.assertEqual(collector._gpu_process_utilization("gpu", {1}), {})

    def test_gpu_process_union_joins_host_details_and_preserves_unknowns(self):
        collector = Collector.__new__(Collector)
        collector._nvml = SimpleNamespace(
            NVMLError=RuntimeError, nvmlMemory_v2=2, NVML_TEMPERATURE_GPU=0,
            nvmlDeviceGetCount=lambda: 1, nvmlDeviceGetHandleByIndex=lambda index: index,
            nvmlDeviceGetMemoryInfo=lambda *args: SimpleNamespace(total=1000, used=120),
            nvmlDeviceGetUtilizationRates=lambda handle: SimpleNamespace(gpu=25),
            nvmlDeviceGetPowerUsage=lambda handle: 50000,
            nvmlDeviceGetEnforcedPowerLimit=lambda handle: 100000,
            nvmlDeviceGetComputeRunningProcesses=lambda handle: [
                SimpleNamespace(pid=1, usedGpuMemory=100), SimpleNamespace(pid=2, usedGpuMemory=None)],
            nvmlDeviceGetGraphicsRunningProcesses=lambda handle: [
                SimpleNamespace(pid=1, usedGpuMemory=120), SimpleNamespace(pid=3, usedGpuMemory=(1 << 64) - 1)],
            nvmlSystemGetProcessName=lambda pid: f"gpu-process-{pid}",
            nvmlDeviceGetName=lambda handle: "GPU", nvmlDeviceGetUUID=lambda handle: "GPU-test",
            nvmlDeviceGetTemperature=lambda *args: 40, nvmlDeviceGetFanSpeed=lambda handle: 20,
        )
        host = {"pid": 1, "name": "python", "user": "demo", "cpu": 125.5,
                "memory": 4096, "command": "python train.py"}
        with patch.object(collector, "_gpu_process_utilization", return_value={1: 25.0, 2: 0.0}):
            gpus, status = collector._gpus([host])
        rows = {row["pid"]: row for row in gpus[0]["processes"]}
        self.assertEqual(status, "")
        self.assertEqual(set(rows), {1, 2, 3})
        self.assertEqual(rows[1], {
            "pid": 1, "name": "python", "type": "C+G", "device": 0,
            "memory": 120, "memory_percent": 12.0, "gpu_utilization": 25.0,
            "user": "demo", "cpu": 125.5, "host_memory": 4096, "command": "python train.py",
        })
        self.assertEqual(rows[2]["gpu_utilization"], 0.0)
        self.assertIsNone(rows[2]["memory_percent"])
        for field in ("gpu_utilization", "memory", "memory_percent", "cpu", "host_memory"):
            self.assertIsNone(rows[3][field])
        self.assertEqual(rows[3]["name"], "gpu-process-3")
        self.assertEqual(rows[3]["command"], "")


if __name__ == "__main__":
    unittest.main()
