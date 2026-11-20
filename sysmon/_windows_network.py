"""Read per-process TCP and UDP byte counts from an in-memory Windows ETW session."""

from __future__ import annotations

from collections import deque
import ctypes as c
import os
import struct
from threading import Lock, Thread
import time
import uuid


U16, U32, U64, I32, I64 = c.c_uint16, c.c_uint32, c.c_uint64, c.c_int32, c.c_int64
PTR, GUID = c.c_void_p, c.c_ubyte * 16
SYSTEMTIME = U16 * 8


class _WnodeHeader(c.Structure):
    _fields_ = [
        ("buffer_size", U32), ("provider_id", U32), ("historical_context", U64), ("timestamp", I64),
        ("guid", GUID), ("client_context", U32), ("flags", U32),
    ]


class _TraceProperties(c.Structure):
    _fields_ = [
        ("wnode", _WnodeHeader), ("buffer_size", U32), ("min_buffers", U32), ("max_buffers", U32),
        ("max_file_size", U32), ("mode", U32), ("flush_timer", U32), ("enable_flags", U32),
        ("age_limit", I32), ("number_of_buffers", U32), ("free_buffers", U32), ("events_lost", U32),
        ("buffers_written", U32), ("log_buffers_lost", U32), ("real_time_buffers_lost", U32),
        ("logger_thread_id", PTR), ("log_file_name_offset", U32), ("logger_name_offset", U32),
    ]


class _SessionProperties(c.Structure):
    _fields_ = [("trace", _TraceProperties), ("logger_name", c.c_wchar * 64)]


class _TimeZone(c.Structure):
    _fields_ = [
        ("bias", I32), ("standard_name", c.c_wchar * 32), ("standard_date", SYSTEMTIME),
        ("standard_bias", I32), ("daylight_name", c.c_wchar * 32), ("daylight_date", SYSTEMTIME),
        ("daylight_bias", I32),
    ]


class _TraceLogfileHeader(c.Structure):
    _fields_ = [
        ("buffer_size", U32), ("version", U32), ("provider_version", U32), ("processors", U32),
        ("end_time", I64), ("timer_resolution", U32), ("max_file_size", U32), ("mode", U32),
        ("buffers_written", U32), ("log_instance", GUID), ("logger_name", PTR), ("log_file_name", PTR),
        ("time_zone", _TimeZone), ("boot_time", I64), ("perf_freq", I64), ("start_time", I64),
        ("reserved_flags", U32), ("buffers_lost", U32),
    ]


class _LegacyTraceHeader(c.Structure):
    _fields_ = [
        ("size", U16), ("field_type_flags", U16), ("version", U32), ("thread_id", U32),
        ("process_id", U32), ("timestamp", I64), ("guid", GUID), ("processor_time", U64),
    ]


class _LegacyTraceEvent(c.Structure):
    _fields_ = [
        ("header", _LegacyTraceHeader), ("instance_id", U32), ("parent_instance_id", U32),
        ("parent_guid", GUID), ("mof_data", PTR), ("mof_length", U32), ("client_context", U32),
    ]


class _EventDescriptor(c.Structure):
    _fields_ = [
        ("id", U16), ("version", c.c_ubyte), ("channel", c.c_ubyte), ("level", c.c_ubyte),
        ("opcode", c.c_ubyte), ("task", U16), ("keyword", U64),
    ]


class _EventHeader(c.Structure):
    _fields_ = [
        ("size", U16), ("header_type", U16), ("flags", U16), ("event_property", U16),
        ("thread_id", U32), ("process_id", U32), ("timestamp", I64), ("provider", GUID),
        ("descriptor", _EventDescriptor), ("processor_time", U64), ("activity_id", GUID),
    ]


class _EventRecord(c.Structure):
    _fields_ = [
        ("header", _EventHeader), ("buffer_context", U32), ("extended_count", U16),
        ("data_length", U16), ("extended_data", PTR), ("data", PTR), ("user_context", PTR),
    ]


_EventCallback = c.WINFUNCTYPE(None, c.POINTER(_EventRecord))


class _TraceLogfile(c.Structure):
    _fields_ = [
        ("log_file_name", c.c_wchar_p), ("logger_name", c.c_wchar_p), ("current_time", I64),
        ("buffers_read", U32), ("mode", U32), ("current_event", _LegacyTraceEvent),
        ("log_header", _TraceLogfileHeader), ("buffer_callback", PTR), ("buffer_size", U32),
        ("filled", U32), ("events_lost", U32), ("event_callback", _EventCallback),
        ("is_kernel", U32), ("context", PTR),
    ]


_etw = c.WinDLL("advapi32", use_last_error=True)
_etw.StartTraceW.argtypes = [c.POINTER(U64), c.c_wchar_p, c.POINTER(_TraceProperties)]
_etw.StartTraceW.restype = U32
_etw.ControlTraceW.argtypes = [U64, c.c_wchar_p, c.POINTER(_TraceProperties), U32]
_etw.ControlTraceW.restype = U32
_etw.OpenTraceW.argtypes = [c.POINTER(_TraceLogfile)]
_etw.OpenTraceW.restype = U64
_etw.ProcessTrace.argtypes = [c.POINTER(U64), U32, PTR, PTR]
_etw.ProcessTrace.restype = U32
_etw.CloseTrace.argtypes = [U64]
_etw.CloseTrace.restype = U32

_NETWORK_PROVIDERS = {
    uuid.UUID("9a280ac0-c8e0-11d1-84e2-00c04fb998a2").bytes_le,  # TcpIp
    uuid.UUID("bf3a50c5-a9c9-4988-a005-2df0b7c80f80").bytes_le,  # UdpIp
}
_DIRECTIONS = {10: (4, 1), 11: (4, 0), 26: (16, 1), 27: (16, 0)}


def _loopback(address):
    if len(address) == 4:
        return address[0] == 127
    return address == b"\0" * 15 + b"\1" or address.startswith(b"\0" * 10 + b"\xff\xff\x7f")


class WindowsProcessNetwork:
    scope = "tcp_udp"
    interfaces = None

    def __init__(self):
        self._lock = Lock()
        self._counts = {}
        self._batches = deque()
        self._stamp = time.monotonic()
        self._session = U64()
        self._trace = None
        self._thread = None
        self._status = ""

        identity = uuid.uuid4()
        self._name = f"VitalNetwork-{os.getpid()}-{identity.hex}"
        self._properties = _SessionProperties()
        props = self._properties.trace
        props.wnode.buffer_size = c.sizeof(self._properties)
        props.wnode.guid[:] = identity.bytes_le
        props.wnode.flags = 0x00020000  # WNODE_FLAG_TRACED_GUID
        props.mode = 0x02000100  # EVENT_TRACE_SYSTEM_LOGGER_MODE | EVENT_TRACE_REAL_TIME_MODE
        props.flush_timer = 1
        props.enable_flags = 0x00010000  # EVENT_TRACE_FLAG_NETWORK_TCPIP
        props.logger_name_offset = _SessionProperties.logger_name.offset
        error = _etw.StartTraceW(c.byref(self._session), self._name, c.byref(props))
        if error:
            self._status = "以系統管理員身分開啟 Vital" if error in (5, 1314) else "無法讀取程序流量"
            return

        self._logfile = _TraceLogfile()
        self._logfile.logger_name = self._name
        self._logfile.mode = 0x10000100  # PROCESS_TRACE_MODE_EVENT_RECORD | PROCESS_TRACE_MODE_REAL_TIME
        self._logfile.event_callback = _EventCallback(self._on_event)
        handle = _etw.OpenTraceW(c.byref(self._logfile))
        if handle in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            self._status = "無法讀取程序流量"
            self.close()
            return
        self._trace = U64(handle)
        self._thread = Thread(target=_etw.ProcessTrace, args=(c.byref(self._trace), 1, None, None), daemon=True)
        self._thread.start()

    def _on_event(self, record):
        event = record.contents
        descriptor = event.header.descriptor
        direction = _DIRECTIONS.get(descriptor.opcode)
        if direction is None or descriptor.version != 2 or bytes(event.header.provider) not in _NETWORK_PROVIDERS:
            return
        width, index = direction
        length = 8 + 2 * width
        if not event.data or event.data_length < length:
            return
        # The payload PID is the socket owner. The header can name a kernel thread.
        payload = c.string_at(event.data, length)
        pid, size = struct.unpack_from("<II", payload)
        destination, source = payload[8:8 + width], payload[8 + width:]
        if not pid or pid == 0xFFFFFFFF or not size or source == destination or _loopback(source) or _loopback(destination):
            return
        with self._lock:
            self._counts.setdefault(pid, [0, 0])[index] += size

    def sample(self, _pids):
        if self._status:
            return {}, self._status
        if not self._thread.is_alive():
            return {}, "無法讀取程序流量"
        with self._lock:
            now = time.monotonic()
            self._batches.append((self._stamp, self._counts))
            self._stamp, self._counts = now, {}
        # ETW delivers buffered events in bursts. Include at least one flush interval.
        while len(self._batches) > 1 and self._batches[1][0] <= now - 1.5:
            self._batches.popleft()
        elapsed = now - self._batches[0][0]
        totals = {}
        for _, batch in self._batches:
            for pid, (received, sent) in batch.items():
                prev_received, prev_sent = totals.get(pid, (0, 0))
                totals[pid] = prev_received + received, prev_sent + sent
        if not totals and elapsed < 1:
            return {}, "等待下一次取樣"
        return {pid: (recv / elapsed, sent / elapsed) for pid, (recv, sent) in totals.items()}, ""

    def close(self):
        if self._session.value:
            _etw.ControlTraceW(self._session, None, c.byref(self._properties.trace), 1)  # EVENT_TRACE_CONTROL_STOP
            self._session.value = 0
        if self._trace is not None:
            _etw.CloseTrace(self._trace)
            self._trace = None
            self._thread.join(timeout=1)
