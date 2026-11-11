"""Windows counters that do not come from procfs."""

from collections import namedtuple
import ctypes
from ctypes import wintypes
import math
import platform
import struct
import subprocess
import time
import winreg

import psutil


CpuTimes = namedtuple("CpuTimes", "user system idle irq softirq")


def cpu_times():
    # Windows counts interrupts and DPCs in kernel time. Keep the buckets disjoint.
    cores = {
        index: CpuTimes(row.user, row.system - row.interrupt - row.dpc, row.idle, row.interrupt, row.dpc)
        for index, row in enumerate(psutil.cpu_times(percpu=True))
    }
    return CpuTimes(*map(sum, zip(*cores.values()))), cores


class _GroupAffinity(ctypes.Structure):
    _fields_ = [("mask", ctypes.c_size_t), ("group", wintypes.USHORT), ("reserved", wintypes.USHORT * 3)]


def _affinity_processors(buffer, offset):
    # The processor and NUMA relationships share the same affinity array layout.
    count, = struct.unpack_from("<H", buffer, offset + 22)
    size = ctypes.sizeof(_GroupAffinity)
    for index in range(count or 1):  # Older Windows NUMA records have a single mask and no count.
        affinity = _GroupAffinity.from_buffer(buffer, offset + 24 + index * size)
        yield from ((affinity.group, cpu) for cpu in range(affinity.mask.bit_length())
                    if affinity.mask & (1 << cpu))


def _topology():
    query = ctypes.WinDLL("kernel32").GetLogicalProcessorInformationEx
    query.argtypes = (wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
    query.restype = wintypes.BOOL
    relation_all = 0xffff
    length = wintypes.DWORD()
    query(relation_all, None, ctypes.byref(length))
    if not length.value:
        return {}
    buffer = ctypes.create_string_buffer(length.value)
    if not query(relation_all, buffer, ctypes.byref(length)):
        return {}

    cores, packages, nodes, cache = [], [], {}, {}
    offset = 0
    while offset < length.value:
        relation, size = struct.unpack_from("<II", buffer, offset)
        data = offset + 8
        if relation in (0, 3):  # RelationProcessorCore / RelationProcessorPackage
            (cores if relation == 0 else packages).append(tuple(_affinity_processors(buffer, data)))
        elif relation in (1, 6):  # RelationNumaNode / RelationNumaNodeEx
            node, = struct.unpack_from("<I", buffer, data)
            nodes.update(dict.fromkeys(_affinity_processors(buffer, data), node))
        elif relation == 2:  # RelationCache
            level, _, _, capacity, kind = struct.unpack_from("<BBHII", buffer, data)
            name = {1: "cache_l1i", 2: "cache_l1d"}.get(kind) if level == 1 else f"cache_l{level}"
            if name in ("cache_l1i", "cache_l1d", "cache_l2", "cache_l3"):
                cache[name] = cache.get(name, 0) + capacity
        offset += size

    core_ids = {cpu: index for index, group in enumerate(sorted(cores)) for cpu in group}
    sockets = {cpu: index for index, group in enumerate(sorted(packages)) for cpu in group}
    topology = [
        {"cpu": index, "core": core_ids[cpu], "socket": sockets.get(cpu), "node": nodes.get(cpu)}
        for index, cpu in enumerate(sorted(core_ids))
    ]
    cores_per_socket = {len({core_ids[cpu] for cpu in group if cpu in core_ids}) for group in packages}
    threads_per_core = {len(group) for group in cores}
    return {
        "sockets": str(len(packages)) if packages else None,
        "cores_per_socket": "/".join(map(str, sorted(cores_per_socket))) or None,
        "threads_per_core": "/".join(map(str, sorted(threads_per_core))) or None,
        "topology": topology,
        **{name: f"{capacity / 2**20:g} MiB" if capacity >= 2**20 else f"{capacity / 1024:g} KiB"
           for name, capacity in cache.items()},
    }


def hardware():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            model, _ = winreg.QueryValueEx(key, "ProcessorNameString")
    except OSError:
        model = platform.processor() or None
    return {
        "os": platform.system(), "kernel": platform.version(), "architecture": platform.machine(),
        "cpu_model": model.strip() if model else None, "sockets": None, "cores_per_socket": None,
        "threads_per_core": None, "virtualization": None, "cache_l1d": None, "cache_l1i": None,
        "cache_l2": None, "cache_l3": None, "driver_version": None, "topology": [],
        **_topology(),
    }


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", wintypes.USHORT), ("max_length", wintypes.USHORT), ("buffer", ctypes.c_void_p)]


# SystemProcessInformation returns one header followed by its thread records.
class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("next_entry", wintypes.ULONG), ("threads", wintypes.ULONG),
        ("reserved", ctypes.c_int64 * 3), ("created", ctypes.c_int64),
        ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("name", _UnicodeString), ("priority", wintypes.LONG),
        ("pid", ctypes.c_size_t), ("parent_pid", ctypes.c_size_t),
        ("handle_count", wintypes.ULONG), ("session", wintypes.ULONG),
        ("process_key", ctypes.c_size_t), ("virtual_sizes", ctypes.c_size_t * 2),
        ("page_faults", wintypes.ULONG), ("peak_working_set", ctypes.c_size_t),
        ("working_set", ctypes.c_size_t), ("memory_counters", ctypes.c_size_t * 7),
        ("io_counters", ctypes.c_int64 * 6),
    ]


class _ThreadInfo(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_int64 * 3), ("wait_time", wintypes.ULONG),
        ("start_address", ctypes.c_void_p), ("client_id", ctypes.c_size_t * 2),
        ("priority", wintypes.LONG), ("base_priority", wintypes.LONG),
        ("context_switches", wintypes.ULONG), ("state", wintypes.ULONG),
        ("wait_reason", wintypes.ULONG),
    ]


_query_system = ctypes.WinDLL("ntdll").NtQuerySystemInformation
_query_system.argtypes = (wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))
_query_system.restype = wintypes.LONG
_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = ctypes.c_int32(0xC0000004).value
_TICKS_PER_SECOND = 10_000_000
_UNIX_EPOCH = 116_444_736_000_000_000


def cpu_temperature():
    """Read the CPU package value published by HWiNFO's Gadget export."""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\HWiNFO64\VSB") as key:
                _, count, updated = winreg.QueryInfoKey(key)
                if time.time() - (updated - _UNIX_EPOCH) / _TICKS_PER_SECOND > 10:
                    continue
                values = {name: value for name, value, _ in
                          (winreg.EnumValue(key, index) for index in range(count))}
        except OSError:
            continue
        temperatures = []
        for name, label in values.items():
            if not name.startswith("Label") or label not in ("CPU Package", "CPU封裝", "CPU (Tctl/Tdie)"):
                continue
            index = name[5:]
            sensor = str(values.get("Sensor" + index, ""))
            display = str(values.get("Value" + index, "")).rstrip()
            if not sensor.startswith("CPU [#") or not display.endswith(("°C", "℃", "°F", "℉")):
                continue
            try:
                value = float(str(values.get("ValueRaw" + index, "")).replace(",", "."))
            except ValueError:
                continue
            if math.isfinite(value):
                temperatures.append((value - 32) * 5 / 9 if display.endswith(("°F", "℉")) else value)
        if temperatures:
            return max(temperatures)
    return None


def _process_buffer():
    size = 1024 * 1024
    required = wintypes.ULONG()
    while True:
        buffer = ctypes.create_string_buffer(size)
        result = _query_system(_SYSTEM_PROCESS_INFORMATION, buffer, size, ctypes.byref(required))
        if result >= 0:
            return buffer, required.value
        if result != _STATUS_INFO_LENGTH_MISMATCH:
            raise OSError(f"Cannot read Windows process counters (NTSTATUS 0x{result & 0xffffffff:08x})")
        size = max(required.value, size * 2)


def _process_details(pid, created):
    try:
        process = psutil.Process(pid)
        details = process.as_dict(attrs=("username", "cmdline"))
        if details["username"] or details["cmdline"]:
            started = (created - _UNIX_EPOCH) / _TICKS_PER_SECOND
            # psutil returns the FILETIME as a float, with microsecond rounding.
            if abs(process.create_time() - started) < 0.00001 and process.is_running():
                return details["username"] or "", subprocess.list2cmdline(details["cmdline"] or [])
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return "", ""


def _process_status(buffer, offset, count):
    for index in range(count):
        thread = _ThreadInfo.from_buffer(buffer, offset + index * ctypes.sizeof(_ThreadInfo))
        if thread.state != 5 or thread.wait_reason not in (5, 12):  # Waiting for Suspended or WrSuspended.
            return "running"
    return "stopped" if count else ""


def processes(previous):
    # One kernel snapshot avoids a full system query per protected PID in psutil.
    buffer, length = _process_buffer()
    stamp = time.monotonic()
    base = ctypes.addressof(buffer)
    rows, current = [], {}
    offset = 0
    while True:
        entry = _ProcessInfo.from_buffer(buffer, offset)
        pid = entry.pid
        if pid:  # PID 0 accounts for system idle time.
            image = entry.name
            name = ""
            if image.buffer and base <= image.buffer <= base + length - image.length:
                name = ctypes.wstring_at(image.buffer, image.length // ctypes.sizeof(ctypes.c_wchar))
            user, command = _process_details(pid, entry.created)
            status = _process_status(buffer, offset + ctypes.sizeof(_ProcessInfo), entry.threads)
            identity = (pid, entry.created)
            cpu_time = (entry.user_time + entry.kernel_time) / _TICKS_PER_SECOND
            old = previous.get(identity)
            cpu = None
            if old is not None and stamp > old[0]:
                cpu = max(0.0, (cpu_time - old[1]) / (stamp - old[0]) * 100)
            current[identity] = stamp, cpu_time
            rows.append({
                "pid": pid, "name": name, "user": user, "status": status, "cpu": cpu,
                "memory": entry.working_set, "threads": entry.threads, "command": command,
            })
        if not entry.next_entry:
            return rows, current
        offset += entry.next_entry
