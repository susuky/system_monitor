"""Physical disk activity, WDDM GPU memory and CPU package power from PDH."""

from functools import cache
import ctypes
from ctypes import wintypes
import math
import re
import struct


_pdh = ctypes.WinDLL("pdh.dll")
_handle_ref = ctypes.POINTER(wintypes.HANDLE)
_dword_ref = ctypes.POINTER(wintypes.DWORD)
_pdh.PdhOpenQueryW.argtypes = (wintypes.LPCWSTR, ctypes.c_size_t, _handle_ref)
_pdh.PdhAddEnglishCounterW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, _handle_ref)
_pdh.PdhCollectQueryData.argtypes = (wintypes.HANDLE,)
_pdh.PdhGetFormattedCounterArrayW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, _dword_ref, _dword_ref, ctypes.c_void_p,
)
_pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
_pdh.PdhCloseQuery.argtypes = (wintypes.HANDLE,)

_PDH_MORE_DATA = 0x800007D2
_PDH_FMT_DOUBLE = 0x200
_PDH_FMT_NOSCALE = 0x1000
_PDH_FMT_NOCAP100 = 0x8000
_GPU_INSTANCE = re.compile(r"pid_(\d+)_luid_0x([0-9a-f]+)_0x([0-9a-f]+)_phys_(\d+)(?:#\d+)?", re.IGNORECASE)
_CPU_PACKAGE = re.compile(r"RAPL_Package\d+_PKG", re.IGNORECASE)


class _CounterValue(ctypes.Structure):
    _fields_ = [("status", wintypes.DWORD), ("value", ctypes.c_double)]


class _CounterItem(ctypes.Structure):
    _fields_ = [("name", wintypes.LPWSTR), ("counter", _CounterValue)]


def _values(counter):
    if not counter:
        return []
    fmt = _PDH_FMT_DOUBLE | _PDH_FMT_NOSCALE | _PDH_FMT_NOCAP100
    size = wintypes.DWORD()
    count = wintypes.DWORD()
    if _pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), None) != _PDH_MORE_DATA:
        return []
    buffer = ctypes.create_string_buffer(size.value)
    if _pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), buffer):
        return []
    return [
        (item.name, item.counter.value) for item in (_CounterItem * count.value).from_buffer(buffer)
        if item.counter.status in (0, 1) and math.isfinite(item.counter.value)
    ]


def _disk_busy(values):
    busy = {}
    for name, idle in values:
        number = name.split(" ", 1)[0]
        if number.isdecimal():
            busy[f"PhysicalDrive{number}"] = max(0.0, min(100.0, 100.0 - idle))
    return busy


def _dedicated_memory(values):
    memory = {}
    for name, used in values:
        match = _GPU_INSTANCE.fullmatch(name)
        if match is None or used < 0:
            continue
        pid, high, low, node = match.groups()
        processes = memory.setdefault((int(high, 16), int(low, 16), int(node)), {})
        pid = int(pid)
        # PDH can return duplicate instances for the same process and adapter.
        processes[pid] = max(processes.get(pid, 0), int(used))
    return memory


def _package_power(values):
    # The Energy Meter counter is in milliwatts. Subdomains overlap the package.
    packages = {name.casefold(): power for name, power in values if power >= 0 and _CPU_PACKAGE.fullmatch(name)}
    return sum(packages.values()) / 1000 if packages else None


@cache
def _cuda_driver():
    try:
        cuda = ctypes.WinDLL("nvcuda.dll")
        cuda.cuInit.argtypes = (ctypes.c_uint,)
        cuda.cuDeviceGetByPCIBusId.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.c_char_p)
        cuda.cuDeviceGetLuid.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.c_int)
    except (OSError, AttributeError):
        return None
    return cuda if cuda.cuInit(0) == 0 else None


def _gpu_adapter_key(pci_bus_id):
    cuda = _cuda_driver()
    if cuda is None:
        return None
    device = ctypes.c_int()
    encoded = pci_bus_id.encode("ascii") if isinstance(pci_bus_id, str) else pci_bus_id
    if cuda.cuDeviceGetByPCIBusId(ctypes.byref(device), encoded):
        return None
    luid = ctypes.create_string_buffer(8)
    node_mask = ctypes.c_uint()
    if cuda.cuDeviceGetLuid(luid, ctypes.byref(node_mask), device) or node_mask.value.bit_count() != 1:
        return None
    low, high = struct.unpack("<II", luid.raw)
    return high, low, node_mask.value.bit_length() - 1


class WindowsCounters:
    """Collect Windows performance counters with one query per sampling worker."""

    def __init__(self):
        self._query = wintypes.HANDLE()
        self._adapters = {}
        if _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)):
            self._query = None
            return
        self._disk = self._add(r"\PhysicalDisk(*)\% Idle Time")
        self._memory = self._add(r"\GPU Process Memory(*)\Dedicated Usage")
        self._power = self._add(r"\Energy Meter(*)\Power")

    def _add(self, path):
        handle = wintypes.HANDLE()
        if _pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle)) == 0:
            return handle
        return None

    def gpu_adapter_key(self, pci_bus_id):
        # shortcut: LUIDs stay cached until restart, refresh them if live GPU reconnect is needed.
        if pci_bus_id not in self._adapters:
            self._adapters[pci_bus_id] = _gpu_adapter_key(pci_bus_id)
        return self._adapters[pci_bus_id]

    def sample(self):
        if not self._query or _pdh.PdhCollectQueryData(self._query):
            return {}, {}, None
        return (
            _disk_busy(_values(self._disk)),
            _dedicated_memory(_values(self._memory)),
            _package_power(_values(self._power)),
        )

    def close(self):
        if self._query:
            _pdh.PdhCloseQuery(self._query)
            self._query = None
