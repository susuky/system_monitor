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


if __name__ == "__main__":
    unittest.main()
