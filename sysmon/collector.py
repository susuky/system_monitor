"""Read local system counters without spawning commands during sampling."""

from __future__ import annotations

from collections import namedtuple
import ctypes
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import time
from typing import Any

import psutil

if psutil.WINDOWS:
    from . import _windows
    from ._windows_counters import WindowsCounters
else:
    import pwd


CPU_FIELDS = (
    "user", "system", "idle", "iowait", "irq", "softirq", "nice", "steal",
    "guest", "guest_nice",
)
CpuTimes = namedtuple("CpuTimes", "user nice system idle iowait irq softirq steal guest guest_nice")
PROCESS_STATES = {
    "R": "running", "S": "sleeping", "D": "disk-sleep", "T": "stopped",
    "t": "tracing-stop", "Z": "zombie", "X": "dead", "x": "dead",
    "K": "wake-kill", "W": "waking", "I": "idle", "P": "parked",
}
CPU_SENSORS = {"k10temp", "coretemp", "cpu_thermal", "cpu-thermal", "cpu-therm", "zenpower"}
PSEUDO_FILESYSTEMS = {
    "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs",
    "debugfs", "devpts", "devtmpfs", "efivarfs", "fusectl", "fuse.portal",
    "fuse.gvfsd-fuse", "hugetlbfs", "mqueue", "nsfs", "proc", "pstore",
    "rpc_pipefs", "securityfs", "squashfs", "sysfs", "tmpfs", "tracefs",
}


def _read_cpu_times(path: Path | None = None) -> tuple[CpuTimes, dict[int, CpuTimes]]:
    total, cores = None, {}
    with (path or Path(psutil.PROCFS_PATH) / "stat").open() as source:
        for line in source:
            columns = line.split()
            if not columns or not columns[0].startswith("cpu"):
                continue
            name = columns[0]
            if name != "cpu" and not name[3:].isdigit():
                continue
            values = [int(value) for value in columns[1:11]]
            counters = CpuTimes(*(values + [0] * (10 - len(values))))
            if name == "cpu":
                total = counters
            else:
                cores[int(name[3:])] = counters
    if total is None:
        raise ValueError("/proc/stat 缺少 CPU 計數器")
    return total, cores


def _cpu_usage(current: Any, previous: Any | None) -> tuple[float | None, dict]:
    if previous is None:
        return None, dict.fromkeys(CPU_FIELDS)
    delta = {
        field: max(0.0, getattr(current, field, 0) - getattr(previous, field, 0))
        for field in current._fields
    }
    if "guest" in delta:
        # Linux includes guest time in user/nice; count it only once.
        delta["user"] = max(0.0, delta["user"] - delta["guest"])
        delta["nice"] = max(0.0, delta["nice"] - delta["guest_nice"])
    total = sum(delta.values())
    if total <= 0:
        return None, dict.fromkeys(CPU_FIELDS)
    breakdown = {field: 100 * delta[field] / total if field in delta else None for field in CPU_FIELDS}
    busy = max(0.0, min(100.0, 100 - breakdown["idle"] - (breakdown["iowait"] or 0)))
    return busy, breakdown


def _text(value: Any) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def _read_process_file(directory: int, name: str) -> bytes | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
    except PermissionError:
        return None
    try:
        first = os.read(descriptor, 8192)
        # stat/status are single seq_file records: a short first read contains
        # the whole record. cmdline can return a partial remote-memory read.
        if not first or (name != "cmdline" and len(first) < 8192):
            return first
        chunks = [first]
        while chunk := os.read(descriptor, 8192):
            chunks.append(chunk)
        return b"".join(chunks)
    except PermissionError:
        return None
    finally:
        os.close(descriptor)


def _hardware() -> dict:
    if psutil.WINDOWS:
        return _windows.hardware()
    fields: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["lscpu", "--json"], capture_output=True, text=True, check=True,
            timeout=3, env={**os.environ, "LC_ALL": "C"},
        )
        # Recent lscpu versions can return nested sections.
        def collect(rows: list[dict]) -> None:
            for row in rows:
                if row.get("data") is not None:
                    fields[row["field"].rstrip(":")] = row["data"]
                collect(row.get("children", []))

        collect(json.loads(result.stdout)["lscpu"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass

    topology = []
    for cpu in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"):
        if not cpu.name[3:].isdigit():
            continue
        try:
            nodes = list(cpu.glob("node[0-9]*"))
            topology.append({
                "cpu": int(cpu.name[3:]),
                "core": int((cpu / "topology/core_id").read_text()),
                "socket": int((cpu / "topology/physical_package_id").read_text()),
                "node": int(nodes[0].name[4:]) if nodes else None,
            })
        except (OSError, ValueError):
            continue
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except OSError:
        os_name = platform.system()
    return {
        "os": os_name, "kernel": platform.release(),
        "architecture": fields.get("Architecture", platform.machine()),
        "cpu_model": fields.get("Model name", platform.processor() or None),
        "sockets": fields.get("Socket(s)"),
        "cores_per_socket": fields.get("Core(s) per socket"),
        "threads_per_core": fields.get("Thread(s) per core"),
        "virtualization": fields.get("Hypervisor vendor", fields.get("Virtualization")),
        "cache_l1d": fields.get("L1d cache"), "cache_l1i": fields.get("L1i cache"),
        "cache_l2": fields.get("L2 cache"), "cache_l3": fields.get("L3 cache"),
        "driver_version": None, "topology": sorted(topology, key=lambda row: row["cpu"]),
    }


class Collector:
    """Stateful sampler; call sample sequentially, from any thread."""

    def __init__(self) -> None:
        self.hardware = _hardware()
        kernel = self.hardware["kernel"].lower()
        self._wsl = "microsoft" in kernel or "wsl" in kernel
        self.hostname = socket.gethostname()
        self._logical = psutil.cpu_count() or 1
        self._physical = psutil.cpu_count(logical=False)
        self._boot_time = psutil.boot_time()
        self._previous_cpu = None
        self._previous_cores = {}
        self._previous_processes: dict[tuple, tuple[float, float]] = {}
        self._previous_disk: dict = {}
        self._previous_network: dict = {}
        self._disk_time: float | None = None
        self._network_time: float | None = None
        self._power_previous: tuple[float, list[int]] | None = None
        self._power_domains: list[tuple[Path, int]] = []
        if not psutil.WINDOWS:
            self._clock_ticks = os.sysconf("SC_CLK_TCK")
            for domain in Path("/sys/class/powercap").glob("*"):
                try:
                    if (domain / "name").read_text().strip().startswith("package-"):
                        maximum = int((domain / "max_energy_range_uj").read_text())
                        self._power_domains.append((domain / "energy_uj", maximum))
                except (OSError, ValueError):
                    continue
        self._nvml = None
        self._gpu_status = ""
        self._windows_counters = WindowsCounters() if psutil.WINDOWS else None
        self._init_gpu()

    def _init_gpu(self) -> None:
        self._jetson_gpu = None if psutil.WINDOWS else next(Path("/sys/class/devfreq").glob("*.ga10b"), None)
        self._jetson_temperature = None
        if self._jetson_gpu is not None:
            for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
                try:
                    if (zone / "type").read_text().strip() == "GPU-therm":
                        self._jetson_temperature = zone / "temp"
                        break
                except OSError:
                    continue
            return
        try:
            import pynvml
        except ImportError:
            self._gpu_status = "未安裝 NVIDIA GPU 支援（nvidia-ml-py）"
            return
        try:
            try:
                pynvml.nvmlInit()
            except pynvml.NVMLError_LibraryNotFound:
                if psutil.WINDOWS:
                    raise
                # WSL ships its NVML library outside the usual loader paths.
                wsl_library = Path("/usr/lib/wsl/lib/libnvidia-ml.so.1")
                if not wsl_library.exists():
                    raise
                ctypes.CDLL(str(wsl_library), mode=ctypes.RTLD_GLOBAL)
                pynvml.nvmlInit()
            self._nvml = pynvml
            self.hardware["driver_version"] = _text(pynvml.nvmlSystemGetDriverVersion())
        except (pynvml.NVMLError, OSError) as error:
            self._gpu_status = f"無法讀取 NVIDIA GPU：{error}"

    def close(self) -> None:
        if self._windows_counters is not None:
            self._windows_counters.close()
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except self._nvml.NVMLError:
                pass
            self._nvml = None

    def _cpu_power(self) -> tuple[float | None, str]:
        if not self._power_domains:
            return None, "系統未提供 CPU 功耗感測器"
        now = time.monotonic()
        try:
            energies = [int(path.read_text()) for path, _ in self._power_domains]
        except PermissionError:
            self._power_previous = None
            return None, "無權讀取 CPU 功耗"
        except (OSError, ValueError):
            self._power_previous = None
            return None, "CPU 功耗感測器讀取失敗"
        previous = self._power_previous
        self._power_previous = now, energies
        if previous is None or now <= previous[0]:
            return None, "等待下一次取樣"
        # Sum package domains only; their core/uncore children overlap.
        energy_delta = 0
        for current, old, (_, maximum) in zip(energies, previous[1], self._power_domains):
            delta = current - old
            if delta < 0:
                delta += maximum
            if delta < 0:
                return None, "CPU 功耗計數器已重設"
            energy_delta += delta
        return energy_delta / 1_000_000 / (now - previous[0]), ""

    @staticmethod
    def _cpu_temperature() -> float | None:
        readings = []
        paths = list(Path("/sys/class/hwmon").glob("hwmon*/name"))
        paths.extend(Path("/sys/class/hwmon").glob("hwmon*/device/name"))
        paths.extend(Path("/sys/devices/platform").glob("coretemp.*/hwmon/hwmon*/name"))
        seen = set()
        for name in paths:
            try:
                if name.read_text().strip().lower() not in CPU_SENSORS:
                    continue
                directory = name.parent.resolve()
                if directory in seen:
                    continue
                seen.add(directory)
                sensors, packages = [], []
                for sensor in directory.glob("temp*_input"):
                    try:
                        label = sensor.with_name(sensor.name.replace("_input", "_label")).read_text().strip()
                    except OSError:
                        label = ""
                    sensors.append(sensor)
                    if label in {"Tctl", "Tdie"} or label.startswith("Package"):
                        packages.append(sensor)
                # Only read CPU package values, not other devices or limits.
                values = []
                for candidates in (packages, sensors):
                    for sensor in candidates:
                        try:
                            values.append(float(sensor.read_text()) / 1000)
                        except (OSError, ValueError):
                            continue
                    if values:
                        break
                readings.extend(values)
            except OSError:
                continue
        if not readings:
            for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
                try:
                    if (zone / "type").read_text().strip().lower() in CPU_SENSORS:
                        readings.append(float((zone / "temp").read_text()) / 1000)
                except (OSError, ValueError):
                    continue
        return max(readings) if readings else None

    def _cpu(self, windows_power: float | None = None) -> dict:
        # Read aggregate, IDs and counters together so CPU hotplug cannot
        # pair one core's counters with another core's identity.
        total, cores = _windows.cpu_times() if psutil.WINDOWS else _read_cpu_times()
        core_ids = sorted(cores)
        percent, times = _cpu_usage(total, self._previous_cpu)
        core_results = [
            _cpu_usage(cores[core_id], self._previous_cores.get(core_id))
            for core_id in core_ids
        ]
        self._previous_cpu, self._previous_cores = total, cores
        try:
            frequency = psutil.cpu_freq()
        except (OSError, NotImplementedError):
            frequency = None
        if psutil.WINDOWS:
            temperature, power = _windows.cpu_temperature(), windows_power
            power_status = ""
            if power is None:
                power_status = "等待下一次取樣" if percent is None else "系統未提供 CPU 功耗感測器"
        else:
            temperature = self._cpu_temperature()
            power, power_status = self._cpu_power()
        return {
            "percent": percent, "per_core": [row[0] for row in core_results],
            "core_ids": core_ids,
            "times": times, "per_core_times": [row[1] for row in core_results],
            "logical": len(cores) or self._logical, "physical": self._physical,
            "frequency_mhz": frequency.current if frequency else None,
            "load": None if psutil.WINDOWS else list(os.getloadavg()),
            "temperature": temperature,
            "power_watts": power, "power_status": power_status,
        }

    @staticmethod
    def _memory() -> dict:
        memory = psutil.virtual_memory()
        try:
            swap = psutil.swap_memory()
        except (OSError, RuntimeError):
            # Windows page-file performance counters may be disabled.
            if not psutil.WINDOWS:
                raise
            swap = None
        missing = None if psutil.WINDOWS else 0
        return {
            "total": memory.total, "available": memory.available,
            "used": memory.total - memory.available, "free": memory.free,
            # psutil's Linux cached field already includes SReclaimable.
            "cached": getattr(memory, "cached", missing), "buffers": getattr(memory, "buffers", missing),
            "shared": getattr(memory, "shared", missing), "percent": memory.percent,
            "swap": {key: getattr(swap, key, None) for key in ("total", "used", "free", "percent")},
        }

    def _processes(self) -> list[dict]:
        if psutil.WINDOWS:
            rows, self._previous_processes = _windows.processes(self._previous_processes)
            return rows
        rows, current, users = [], {}, {}
        with os.scandir(psutil.PROCFS_PATH) as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                row = {"pid": pid, "name": "", "user": "", "status": "",
                       "cpu": None, "memory": None, "threads": None, "command": ""}
                try:
                    # A proc directory FD stays bound to this process even if
                    # its PID is reused while reading the remaining files.
                    directory = os.open(entry.path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                except PermissionError:
                    rows.append(row)
                    continue
                except (FileNotFoundError, ProcessLookupError):
                    continue
                try:
                    stat = _read_process_file(directory, "stat")
                    # comm can contain spaces, newlines and closing parentheses.
                    end = stat.rfind(b")") if stat else 0
                    fields = stat[end + 2:].split() if stat else []
                    status = _read_process_file(directory, "status")
                    # PF_KTHREAD with no address space has an empty cmdline.
                    kernel_thread = fields and int(fields[6]) & 0x00200000 and fields[20] == b"0"
                    cmdline = None if kernel_thread else _read_process_file(directory, "cmdline")
                except (FileNotFoundError, ProcessLookupError):
                    continue
                finally:
                    os.close(directory)
                stamp = time.monotonic()
                if stat is not None:
                    row["name"] = os.fsdecode(stat[stat.find(b"(") + 1:end])
                    row["status"] = PROCESS_STATES.get(os.fsdecode(fields[0]), "?")
                    row["threads"] = int(fields[17])
                    identity = (pid, int(fields[19]))
                    cpu_time = (int(fields[11]) + int(fields[12])) / self._clock_ticks
                    previous = self._previous_processes.get(identity)
                    if previous is not None and stamp > previous[0]:
                        row["cpu"] = max(0.0, (cpu_time - previous[1]) / (stamp - previous[0]) * 100)
                    current[identity] = stamp, cpu_time
                if status is not None:
                    # VmRSS matches statm, while stat's RSS may use approximate
                    # counters. Kernel threads and zombies have no VmRSS.
                    row["memory"] = 0
                    start = status.find(b"\nUid:")
                    if start >= 0:
                        end = status.find(b"\n", start + 1)
                        uid = int(status[start + 5:end].split(None, 1)[0])
                        if uid not in users:
                            try:
                                users[uid] = pwd.getpwuid(uid).pw_name
                            except KeyError:
                                users[uid] = str(uid)
                        row["user"] = users[uid]
                    start = status.find(b"\nVmRSS:")
                    if start >= 0:
                        end = status.find(b"\n", start + 1)
                        row["memory"] = int(status[start + 7:end].split(None, 1)[0]) * 1024
                if cmdline:
                    arguments = os.fsdecode(cmdline)
                    separator = "\0" if arguments.endswith("\0") else " "
                    if arguments.endswith(separator):
                        arguments = arguments[:-1]
                    arguments = arguments.split(separator)
                    if len(arguments) == 1 and " " in arguments[0]:
                        arguments = arguments[0].split(" ")
                    row["command"] = " ".join(arguments)
                    expanded = os.path.basename(arguments[0])
                    if len(row["name"]) >= 15 and expanded.startswith(row["name"]):
                        row["name"] = expanded
                rows.append(row)
        self._previous_processes = current
        return rows

    @staticmethod
    def _disks() -> list[dict]:
        rows, seen = [], set()
        for partition in psutil.disk_partitions(all=not psutil.WINDOWS):
            if (partition.mountpoint in seen or partition.fstype in PSEUDO_FILESYSTEMS
                    or partition.device.startswith(("/dev/loop", "/dev/ram"))
                    or (partition.fstype == "overlay" and partition.mountpoint != "/")):
                continue
            seen.add(partition.mountpoint)
            row = {"device": partition.device, "mountpoint": partition.mountpoint, "fstype": partition.fstype}
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                row.update({key: getattr(usage, key) for key in ("total", "used", "free", "percent")})
            except OSError as error:
                row.update(dict.fromkeys(("total", "used", "free", "percent")))
                row["error"] = "無權讀取" if isinstance(error, PermissionError) else "無法讀取檔案系統"
            rows.append(row)
        return sorted(rows, key=lambda row: (row["mountpoint"] != "/", row["mountpoint"]))

    def _disk_io(self, windows_busy: dict[str, float] | None = None) -> list[dict]:
        counters = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        now = time.monotonic()
        elapsed = now - self._disk_time if self._disk_time is not None else None
        rows = []
        for name, counter in counters.items():
            if not psutil.WINDOWS and (name.startswith(("loop", "ram")) or (Path("/sys/class/block") / name / "partition").exists()):
                continue
            previous = self._previous_disk.get(name)
            row = {"name": name}
            for output, field in (("read_rate", "read_bytes"), ("write_rate", "write_bytes"),
                                  ("read_iops", "read_count"), ("write_iops", "write_count"),
                                  ("busy_percent", "busy_time")):
                value = None
                if previous is not None and elapsed and hasattr(counter, field):
                    delta = getattr(counter, field) - getattr(previous, field)
                    if delta >= 0:
                        value = delta / elapsed
                        if output == "busy_percent":
                            value = min(100.0, value / 10)
                row[output] = value
            if windows_busy is not None:
                row["busy_percent"] = windows_busy.get(name)
            rows.append(row)
        self._previous_disk, self._disk_time = counters, now
        return rows

    def _network(self) -> list[dict]:
        counters = psutil.net_io_counters(pernic=True, nowrap=False)
        now = time.monotonic()
        elapsed = now - self._network_time if self._network_time is not None else None
        addresses, stats = psutil.net_if_addrs(), psutil.net_if_stats()
        rows = []
        for name, counter in counters.items():
            previous = self._previous_network.get(name)
            row = {
                "name": name, "up": stats[name].isup if name in stats else False,
                "addresses": [addr.address for addr in addresses.get(name, [])
                              if addr.family in (socket.AF_INET, socket.AF_INET6)],
                "received": counter.bytes_recv, "sent": counter.bytes_sent,
            }
            for output, field in (("recv_rate", "bytes_recv"), ("send_rate", "bytes_sent")):
                delta = getattr(counter, field) - getattr(previous, field) if previous is not None else None
                row[output] = delta / elapsed if delta is not None and delta >= 0 and elapsed else None
            rows.append(row)
        self._previous_network, self._network_time = counters, now
        return sorted(rows, key=lambda row: (not row["up"], row["name"] == "lo", row["name"]))

    def _gpu_process_utilization(self, handle: Any, pids: set[int]) -> dict[int, float]:
        # WSL NVML utilization samples can carry Windows PIDs while running
        # process lists carry Linux PIDs. A numeric match is not an identity.
        if self._wsl or not pids:
            return {}
        # NVML keeps its own sampled history. Re-read the recent window so
        # sub-second refreshes reuse a fresh sample without retaining stale PIDs.
        oldest = time.time_ns() // 1000 - 2_000_000
        try:
            samples = self._nvml.nvmlDeviceGetProcessUtilization(handle, oldest)
        except self._nvml.NVMLError:
            # NotFound means no samples; NotSupported includes MIG devices.
            # Neither supplies a measured 0% for a particular process.
            return {}
        now = time.time_ns() // 1000
        latest = {}
        for sample in samples:
            if (sample.pid in pids and oldest < sample.timeStamp <= now
                    and 0 <= sample.smUtil <= 100
                    and (sample.pid not in latest or sample.timeStamp > latest[sample.pid].timeStamp)):
                latest[sample.pid] = sample
        return {pid: float(sample.smUtil) for pid, sample in latest.items()}

    def _gpus(
        self, processes: list[dict], windows_vram: dict[tuple[int, int, int], dict[int, int]] | None = None,
    ) -> tuple[list[dict], str]:
        nvml = self._nvml
        if nvml is None:
            if self._jetson_gpu is not None:
                return self._jetson_gpus()
            return [], self._gpu_status

        def query(function: Any, *args: Any) -> Any:
            try:
                return function(*args)
            except nvml.NVMLError:
                return None

        count = query(nvml.nvmlDeviceGetCount)
        if count is None:
            return [], "無法讀取 NVIDIA GPU"
        if count == 0:
            return [], "未偵測到 NVIDIA GPU"
        host_processes = {row["pid"]: row for row in processes}
        gpus, errors = [], []
        for index in range(count):
            handle = query(nvml.nvmlDeviceGetHandleByIndex, index)
            if handle is None:
                errors.append(f"GPU {index} 無法讀取")
                continue
            # v2 excludes driver-reserved memory from used, like nvidia-smi.
            memory = query(nvml.nvmlDeviceGetMemoryInfo, handle, nvml.nvmlMemory_v2)
            if memory is None:  # Drivers predating the v2 API expose v1 only.
                memory = query(nvml.nvmlDeviceGetMemoryInfo, handle)
            utilization = query(nvml.nvmlDeviceGetUtilizationRates, handle)
            power = query(nvml.nvmlDeviceGetPowerUsage, handle)
            limit = query(nvml.nvmlDeviceGetEnforcedPowerLimit, handle)
            gpu_processes: dict[int, dict] = {}
            sources = (("C", nvml.nvmlDeviceGetComputeRunningProcesses),
                       ("G", nvml.nvmlDeviceGetGraphicsRunningProcesses))
            for kind, function in sources:
                running = query(function, handle)
                if running is None:
                    errors.append(f"GPU {index} 無法讀取 {kind} 程序資料")
                for process in running or []:
                    used = process.usedGpuMemory
                    if used is not None and used >= (1 << 64) - 1:
                        used = None
                    if process.pid in gpu_processes:
                        row = gpu_processes[process.pid]
                        if kind not in row["type"]:
                            row["type"] = "C+G"
                        if used is not None:
                            row["memory"] = max(row["memory"] or 0, used)
                    else:
                        host = host_processes.get(process.pid, {})
                        name = host.get("name") or query(nvml.nvmlSystemGetProcessName, process.pid)
                        gpu_processes[process.pid] = {
                            "pid": process.pid, "name": _text(name) if name else "",
                            "memory": used, "type": kind, "device": index,
                            "user": host.get("user", ""), "cpu": host.get("cpu"),
                            "host_memory": host.get("memory"), "command": host.get("command", ""),
                        }
            process_usage = self._gpu_process_utilization(handle, set(gpu_processes))
            dedicated = {}
            if windows_vram and any(row["memory"] is None for row in gpu_processes.values()):
                pci = query(nvml.nvmlDeviceGetPciInfo, handle)
                if pci is not None:
                    adapter = self._windows_counters.gpu_adapter_key(pci.busId)
                    dedicated = windows_vram.get(adapter, {})
            for pid, row in gpu_processes.items():
                if row["memory"] is None:
                    row["memory"] = dedicated.get(pid)
                row["gpu_utilization"] = process_usage.get(pid)
                row["memory_percent"] = (
                    row["memory"] / memory.total * 100
                    if row["memory"] is not None and memory is not None and memory.total else None
                )
            name = query(nvml.nvmlDeviceGetName, handle)
            uuid = query(nvml.nvmlDeviceGetUUID, handle)
            gpus.append({
                "index": index, "name": _text(name) if name else f"GPU {index}",
                "uuid": _text(uuid) if uuid else "",
                "utilization": utilization.gpu if utilization else None,
                "memory_used": memory.used if memory else None,
                "memory_total": memory.total if memory else None,
                "temperature": query(nvml.nvmlDeviceGetTemperature, handle, nvml.NVML_TEMPERATURE_GPU),
                "power_watts": power / 1000 if power is not None else None,
                "power_limit_watts": limit / 1000 if limit is not None else None,
                "fan_percent": query(nvml.nvmlDeviceGetFanSpeed, handle),
                "processes": sorted(gpu_processes.values(), key=lambda row: row["pid"]),
            })
        if self._wsl and gpus:
            errors.append("WSL 未提供完整 GPU 程序清單")
        return gpus, "；".join(errors)

    def _jetson_gpus(self) -> tuple[list[dict], str]:
        # Orin's nvgpu driver exposes load in per-mille, without NVML.
        utilization, temperature = None, None
        status = "Jetson GPU 程序資料不可用"
        try:
            load = int((self._jetson_gpu / "device/load").read_text())
            if not 0 <= load <= 1000:
                raise ValueError("Invalid GPU load")
            utilization = load / 10
        except (OSError, ValueError):
            status += "；無法讀取 Jetson GPU 使用率"
        if self._jetson_temperature is not None:
            try:
                temperature = int(self._jetson_temperature.read_text()) / 1000
            except (OSError, ValueError):
                pass
        return [{
            "index": 0, "name": "NVIDIA Orin GPU", "uuid": "",
            "utilization": utilization, "temperature": temperature,
            "memory_shared": True, "memory_used": None, "memory_total": None,
            "power_watts": None, "power_limit_watts": None, "fan_percent": None,
            "processes": [], "processes_supported": False,
        }], status

    def sample(self) -> dict:
        timestamp = time.time()
        busy, vram, power = self._windows_counters.sample() if self._windows_counters is not None else (None, None, None)
        cpu, memory = self._cpu(power), self._memory()
        processes = self._processes()
        gpus, gpu_status = self._gpus(processes, vram)
        return {
            "timestamp": timestamp, "hostname": self.hostname,
            "uptime": max(0.0, timestamp - self._boot_time),
            "cpu": cpu, "memory": memory, "processes": processes,
            "disks": self._disks(), "disk_io": self._disk_io(busy),
            "network": self._network(), "gpus": gpus, "gpu_status": gpu_status,
            "hardware": self.hardware,
        }
