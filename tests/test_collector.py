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


linux_only = unittest.skipUnless(sys.platform.startswith("linux"), "uses Linux procfs or sysfs")


class CollectorTests(unittest.TestCase):
    def test_live_counters_and_process_cpu(self):
        collector = Collector()
        # A Windows virtualenv launcher has its own PID and waits for Python.
        child = subprocess.Popen([sys._base_executable, "-c", "while True: pass", "Vital 測試"])
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
                    self.assertAlmostEqual(sum(value for value in row.values() if value is not None), 100)
            processes = {row["pid"]: row for row in second["processes"]}
            self.assertGreater(processes[child.pid]["cpu"], 0)
            self.assertGreater(processes[child.pid]["memory"], 0)
            memory = second["memory"]
            self.assertEqual(memory["used"] + memory["available"], memory["total"])
            if sys.platform == "win32":
                self.assertEqual(second["hardware"]["os"], "Windows")
                self.assertIsNone(second["cpu"]["load"])
                self.assertIsNone(second["cpu"]["times"]["iowait"])
                self.assertNotIn(0, processes)
                self.assertIn("Vital 測試", processes[child.pid]["command"])
                self.assertTrue(any(Path(row["mountpoint"]).drive for row in second["disks"]))
            else:
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

    @linux_only
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

    def test_cpu_temperature_package_selection_changes_and_thermal_zones(self):
        with tempfile.TemporaryDirectory(prefix="vital-temperature-") as temporary:
            root = Path(temporary)
            cpu = root / "sys/class/hwmon/hwmon0"
            disk = root / "sys/class/hwmon/hwmon1"
            zone = root / "sys/class/thermal/thermal_zone0"
            for directory in (cpu, disk, zone):
                directory.mkdir(parents=True)
            (cpu / "name").write_text("coretemp\n")
            (cpu / "temp1_label").write_text("Package id 0\n")
            (cpu / "temp1_input").write_text("51000\n")
            (cpu / "temp2_label").write_text("Core 0\n")
            (cpu / "temp2_input").write_text("60000\n")
            (disk / "name").write_text("nvme\n")
            (disk / "temp1_input").write_text("90000\n")
            (zone / "type").write_text("CPU-therm\n")
            (zone / "temp").write_text("47000\n")
            with patch("sysmon.collector.Path", side_effect=lambda path: root / path.lstrip("/")):
                self.assertEqual(Collector._cpu_temperature(), 51)
                (cpu / "temp1_input").write_text("52500\n")
                self.assertEqual(Collector._cpu_temperature(), 52.5)
                (cpu / "temp1_input").write_text("unavailable\n")
                self.assertEqual(Collector._cpu_temperature(), 60)
                (cpu / "temp1_input").write_text("52500\n")
                (cpu / "temp1_label").unlink()
                self.assertEqual(Collector._cpu_temperature(), 60)
                (cpu / "name").unlink()
                self.assertEqual(Collector._cpu_temperature(), 47)
                (zone / "temp").unlink()
                self.assertIsNone(Collector._cpu_temperature())

    def test_gpu_utilization_requires_recent_valid_samples(self):
        collector = Collector.__new__(Collector)
        collector._wsl = False
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
            collector._wsl = True
            with patch.object(collector._nvml, "nvmlDeviceGetProcessUtilization") as query:
                self.assertEqual(collector._gpu_process_utilization("gpu", {1}), {})
                query.assert_not_called()

    def test_orin_load_units_shared_memory_and_unreadable_sensor(self):
        collector = Collector.__new__(Collector)
        collector._nvml = None
        with tempfile.TemporaryDirectory(prefix="vital-orin-") as directory:
            collector._jetson_gpu = Path(directory)
            (Path(directory) / "device").mkdir()
            load = Path(directory) / "device/load"
            load.write_text("735\n")
            collector._jetson_temperature = Path(directory) / "temp"
            collector._jetson_temperature.write_text("51250\n")
            gpus, status = collector._gpus([])
            self.assertEqual(gpus[0]["utilization"], 73.5)
            self.assertEqual(gpus[0]["temperature"], 51.25)
            self.assertTrue(gpus[0]["memory_shared"])
            self.assertFalse(gpus[0]["processes_supported"])
            self.assertIsNone(gpus[0]["memory_used"])
            self.assertIsNone(gpus[0]["memory_total"])
            self.assertIn("GPU 程序資料不可用", status)
            for value in ("0", "1000", "1001", "unavailable"):
                load.write_text(value)
                gpus, status = collector._gpus([])
                expected = {"0": 0.0, "1000": 100.0}.get(value)
                self.assertEqual(gpus[0]["utilization"], expected)
            load.unlink()
            collector._jetson_temperature.unlink()
            gpus, status = collector._gpus([])
            self.assertIsNone(gpus[0]["utilization"])
            self.assertIsNone(gpus[0]["temperature"])
            self.assertIn("無法讀取 Jetson GPU 使用率", status)

    @linux_only
    def test_proc_fields_refresh_and_pid_reuse_with_nonstandard_clock(self):
        collector = Collector.__new__(Collector)
        collector._previous_processes = {}
        collector._clock_ticks = 250

        def write_process(directory, name, start, ticks, uid, command):
            fields = ["0"] * 22
            fields[0], fields[11], fields[12] = "S", str(ticks), "125"
            fields[17], fields[19], fields[21] = "3", str(start), "8"
            (directory / "stat").write_bytes(f"17 ({name}) {' '.join(fields)}\n".encode())
            groups = "1000 " * 4000
            (directory / "status").write_text(f"Name:\t{name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGroups:\t{groups}\nVmRSS:\t1024 kB\n")
            (directory / "cmdline").write_bytes(command)

        with tempfile.TemporaryDirectory(prefix="vital-proc-") as temporary:
            root = Path(temporary)
            directory = root / "17"
            directory.mkdir()
            name = "odd) name\npart"
            command = b"/usr/bin/example\0" + b"x" * 100_000 + b"\0"
            write_process(directory, name, 1000, 250, 12345, command)
            with patch("sysmon.collector.psutil.PROCFS_PATH", temporary), \
                    patch("sysmon.collector.pwd.getpwuid", side_effect=lambda uid: SimpleNamespace(pw_name=f"user-{uid}")), \
                    patch("sysmon.collector.time.monotonic", side_effect=[10, 11, 12, 13]):
                first = collector._processes()[0]
                self.assertEqual(first["name"], name)
                self.assertEqual(first["memory"], 1024 * 1024)
                self.assertEqual(first["threads"], 3)
                self.assertEqual(first["command"], "/usr/bin/example " + "x" * 100_000)
                self.assertIsNone(first["cpu"])
                write_process(directory, "long-process-na", 1000, 375, 12346, b"/usr/bin/long-process-name\0--changed\0")
                updated = collector._processes()[0]
                self.assertEqual(updated["cpu"], 50)
                self.assertEqual(updated["name"], "long-process-name")
                self.assertEqual(updated["command"], "/usr/bin/long-process-name --changed")
                self.assertEqual(updated["user"], "user-12346")
                write_process(directory, "new", 2000, 0, 12346, b"new\0")
                self.assertIsNone(collector._processes()[0]["cpu"])
                (directory / "status").write_text("Name:\tnew\nUid:\t12346\t12346\t12346\t12346\n")
                self.assertEqual(collector._processes()[0]["memory"], 0)

    @linux_only
    def test_kernel_threads_skip_empty_cmdline_but_keep_user_and_cpu(self):
        collector = Collector.__new__(Collector)
        collector._previous_processes = {}
        collector._clock_ticks = 100
        with tempfile.TemporaryDirectory(prefix="vital-kthread-") as temporary:
            directory = Path(temporary) / "17"
            directory.mkdir()
            fields = ["0"] * 22
            fields[0], fields[6], fields[17], fields[19] = "S", str(0x00200000), "1", "100"
            (directory / "stat").write_text(f"17 (kworker) {' '.join(fields)}\n")
            (directory / "status").write_text("Name:\tkworker\nUid:\t12345\t12345\t12345\t12345\n")
            # No cmdline file: sampling must not try to open it for this task.
            with patch("sysmon.collector.psutil.PROCFS_PATH", temporary), \
                    patch("sysmon.collector.pwd.getpwuid", side_effect=KeyError), \
                    patch("sysmon.collector.time.monotonic", side_effect=[10, 11, 12]):
                first = collector._processes()[0]
                self.assertEqual(first["user"], "12345")
                self.assertEqual(first["memory"], 0)
                self.assertEqual(first["command"], "")
                self.assertIsNone(first["cpu"])
                fields[11] = "25"
                (directory / "stat").write_text(f"17 (kworker) {' '.join(fields)}\n")
                self.assertEqual(collector._processes()[0]["cpu"], 25)
                fields[6] = "0"
                (directory / "stat").write_text(f"17 (user-task) {' '.join(fields)}\n")
                (directory / "cmdline").write_bytes(b"user-task\0--changed\0")
                self.assertEqual(collector._processes()[0]["command"], "user-task --changed")

    @linux_only
    def test_proc_directory_identity_permissions_and_disappearance(self):
        from sysmon.collector import _read_process_file

        collector = Collector.__new__(Collector)
        collector._previous_processes = {}
        collector._clock_ticks = 100
        with tempfile.TemporaryDirectory(prefix="vital-proc-") as temporary:
            directory = Path(temporary) / "17"
            directory.mkdir()
            fields = ["0"] * 22
            fields[0], fields[17], fields[19], fields[21] = "S", "1", "100", "4"
            (directory / "stat").write_text(f"17 (original) {' '.join(fields)}\n")
            (directory / "status").write_text("Name:\toriginal\nUid:\t12345\t12345\t12345\t12345\nVmRSS:\t16 kB\n")
            (directory / "cmdline").write_bytes(b"original\0")

            def replace_pid_after_stat(descriptor, name):
                data = _read_process_file(descriptor, name)
                if name == "stat":
                    directory.rename(Path(temporary) / "old")
                    directory.mkdir()
                    (directory / "cmdline").write_bytes(b"replacement\0")
                return data

            with patch("sysmon.collector.psutil.PROCFS_PATH", temporary), \
                    patch("sysmon.collector.pwd.getpwuid", side_effect=KeyError):
                with patch("sysmon.collector._read_process_file", side_effect=replace_pid_after_stat):
                    row = collector._processes()[0]
                self.assertEqual(row["command"], "original")
                self.assertEqual(row["user"], "12345")
                self.assertEqual(row["memory"], 4 * 4096)
                # The replacement lacks stat/status and disappears from sampling.
                self.assertEqual(collector._processes(), [])
                for name in ("stat", "status"):
                    (directory / name).write_bytes((Path(temporary) / "old" / name).read_bytes())
                real_open = os.open

                def denied(name, *args, **kwargs):
                    if name in {"stat", "status", "cmdline"}:
                        raise PermissionError(name)
                    return real_open(name, *args, **kwargs)

                with patch("sysmon.collector.os.open", side_effect=denied):
                    restricted = collector._processes()[0]
                self.assertEqual(restricted["pid"], 17)
                self.assertIsNone(restricted["cpu"])
                self.assertIsNone(restricted["memory"])
                self.assertIsNone(restricted["threads"])
                self.assertEqual(restricted["name"], "")
                self.assertEqual(restricted["user"], "")
                self.assertEqual(restricted["command"], "")

    def test_gpu_process_union_joins_host_details_and_preserves_unknowns(self):
        collector = Collector.__new__(Collector)
        collector._wsl = False
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
        collector._wsl = True
        gpus, status = collector._gpus([host])
        self.assertEqual(status, "WSL 未提供完整 GPU 程序清單")
        self.assertEqual(gpus[0]["utilization"], 25)
        self.assertTrue(all(row["gpu_utilization"] is None for row in gpus[0]["processes"]))


if __name__ == "__main__":
    unittest.main()
