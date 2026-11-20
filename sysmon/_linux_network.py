"""Measure Linux process traffic from packet headers or TCP socket counters."""

from __future__ import annotations

from ipaddress import ip_address
import os
from pathlib import Path
import socket
import struct
import threading
import time

import psutil


_NETLINK_HEADER = struct.Struct("=IHHII")
_ATTRIBUTE_HEADER = struct.Struct("=HH")
_PORTS = struct.Struct("!HH")
_IPV4, _IPV6 = 0x0800, 0x86DD
_TCP, _UDP = socket.IPPROTO_TCP, socket.IPPROTO_UDP


def _dump_sockets(diag, family, protocol, *, tcp_info=False):
    # SOCK_DIAG_BY_FAMILY (20), NLM_F_REQUEST | NLM_F_DUMP (0x301).
    # Skip TCP_TIME_WAIT and TCP_LISTEN. The TCP counter path requests INET_DIAG_INFO.
    states = 0xFFFFFFFF & ~((1 << 6) | (1 << 10)) if protocol == _TCP else 0xFFFFFFFF
    request = struct.pack("=4BI48x", family, protocol, 2 if tcp_info else 0, 0, states)
    diag.sendto(_NETLINK_HEADER.pack(16 + len(request), 20, 0x301, family, 0) + request, (0, 0))
    while True:
        data, _, flags, sender = diag.recvmsg(65536)
        if sender[0] != 0:
            continue
        if flags & socket.MSG_TRUNC:
            raise OSError("Truncated socket diagnostics")
        offset = 0
        while offset < len(data):
            length, kind, flags, sequence, _ = _NETLINK_HEADER.unpack_from(data, offset)
            if sequence != family or length < 16 or offset + length > len(data) or flags & 0x10:
                raise OSError("Incomplete socket diagnostics")
            payload = data[offset + 16:offset + length]
            offset += (length + 3) & ~3
            if kind == 3:  # NLMSG_DONE can include an error from the dump.
                if payload and struct.unpack_from("=i", payload)[0]:
                    raise OSError("Socket diagnostics failed")
                return
            if kind == 2:  # NLMSG_ERROR
                raise OSError("Socket diagnostics failed")
            if kind == 20:
                yield payload


def _tcp_counters():
    counters = {}
    # NETLINK_SOCK_DIAG (4) is readable without root in the current namespace.
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as diag:
        diag.settimeout(0.25)
        for family, width in ((socket.AF_INET, 4), (socket.AF_INET6, 16)):
            for payload in _dump_sockets(diag, family, _TCP, tcp_info=True):
                # inet_diag_msg: idiag_cookie at 44, idiag_inode at 68, then attributes.
                inode, = struct.unpack_from("=I", payload, 68)
                if not inode:
                    continue
                local, peer = (ip_address(payload[start:start + width]) for start in (8, 24))
                local = getattr(local, "ipv4_mapped", None) or local
                peer = getattr(peer, "ipv4_mapped", None) or peer
                if local == peer or local.is_loopback or peer.is_loopback:
                    continue
                offset = 72
                while offset + 4 <= len(payload):
                    length, kind = _ATTRIBUTE_HEADER.unpack_from(payload, offset)
                    if length < 4 or offset + length > len(payload):
                        raise OSError("Invalid socket diagnostics attribute")
                    if kind == 2 and length >= 140:  # INET_DIAG_INFO contains struct tcp_info.
                        info = offset + 4
                        received, = struct.unpack_from("=Q", payload, info + 128)
                        # Older kernels expose acknowledged bytes before bytes_sent was added.
                        sent, = struct.unpack_from("=Q", payload, info + (200 if length >= 212 else 120))
                        cookie, = struct.unpack_from("=Q", payload, 44)
                        counters[inode, cookie] = received, sent
                        break
                    offset += (length + 3) & ~3
    return counters


def _owners(inodes, pids):
    targets = {f"socket:[{inode}]": inode for inode in inodes}
    owners = {}
    for pid in pids:
        try:
            with os.scandir(f"{psutil.PROCFS_PATH}/{pid}/fd") as files:
                for fd in files:
                    try:
                        inode = targets.get(os.readlink(fd.path))
                    except FileNotFoundError:
                        continue
                    if inode is not None and owners.setdefault(inode, pid) != pid:
                        # Shared sockets have no single process to credit with the traffic.
                        owners[inode] = None
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return owners


def _normalize(address):
    return address[12:] if len(address) == 16 and address.startswith(b"\0" * 10 + b"\xff\xff") else address


def _interface_ips():
    interfaces = {}
    for name, addresses in psutil.net_if_addrs().items():
        if name == "lo" or Path("/sys/class/net", name, "master").exists():
            continue  # A bridge/bond port can also appear on its master.
        ips = [ip_address(addr.address.partition("%")[0]) for addr in addresses
               if addr.family in (socket.AF_INET, socket.AF_INET6)]
        if not ips or not all(ip.is_loopback for ip in ips):
            interfaces[name] = {_normalize(ip.packed) for ip in ips}
    return interfaces


def _packet_flow(header, ethertype, outgoing, interface_ips):
    if ethertype == _IPV4 and len(header) >= 20 and header[0] >> 4 == 4:
        offset = (header[0] & 15) * 4
        if offset < 20 or header[6] & 0x1F or header[7]:
            return None  # Noninitial fragments do not contain ports.
        source, destination = bytes(header[12:16]), bytes(header[16:20])
        protocol = header[9]
    elif ethertype == _IPV6 and len(header) >= 40 and header[0] >> 4 == 6:
        source, destination = bytes(header[8:24]), bytes(header[24:40])
        offset, protocol = 40, header[6]
        # Common IPv6 extension headers. Leave truncated/unknown chains unassigned.
        while protocol in (0, 43, 44, 51, 60) and offset + 8 <= len(header):
            size = (header[offset + 1] + 1) * 8
            if protocol == 44:
                if header[offset + 2] or header[offset + 3] & 0xF8:
                    return None
                size = 8
            elif protocol == 51:
                size = (header[offset + 1] + 2) * 4
            protocol, offset = header[offset], offset + size
    else:
        return None
    if protocol not in (_TCP, _UDP) or offset + _PORTS.size > len(header):
        return None
    port_source, port_destination = _PORTS.unpack_from(header, offset)
    local, local_port, peer, peer_port = (
        (source, port_source, destination, port_destination) if outgoing else
        (destination, port_destination, source, port_source)
    )
    # Forwarded packets and another interface's copy have no local endpoint here.
    if local not in interface_ips:
        return None
    return protocol, local, local_port, peer, peer_port


def _flow_inodes(protocols):
    inodes = {}
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as diag:
        diag.settimeout(0.25)
        for protocol in protocols:
            for family, width in ((socket.AF_INET, 4), (socket.AF_INET6, 16)):
                for payload in _dump_sockets(diag, family, protocol):
                    inode, = struct.unpack_from("=I", payload, 68)
                    if not inode:
                        continue
                    local = _normalize(payload[8:8 + width])
                    peer = _normalize(payload[24:24 + width])
                    local_port, peer_port = _PORTS.unpack_from(payload, 4)
                    if not peer_port:
                        peer = bytes(len(local))
                    key = protocol, local, local_port, peer, peer_port
                    if inodes.setdefault(key, inode) != inode:
                        inodes[key] = None
                    if protocol == _UDP and local == bytes(16) and not peer_port:
                        # An IPv6 wildcard UDP socket may also accept IPv4.
                        # Linux exposes INET_DIAG_SKV6ONLY for unconnected sockets.
                        offset = 72
                        while offset + 4 <= len(payload):
                            length, kind = _ATTRIBUTE_HEADER.unpack_from(payload, offset)
                            if length < 4 or offset + length > len(payload):
                                break
                            if kind == 11 and length == 5 and payload[offset + 4] == 0:
                                key = _UDP, bytes(4), local_port, bytes(4), 0
                                if inodes.setdefault(key, inode) != inode:
                                    inodes[key] = None
                                break
                            offset += (length + 3) & ~3
    return inodes


def _flow_inode(flow, inodes):
    protocol, local, port, peer, peer_port = flow
    if protocol == _TCP or flow in inodes:
        return inodes.get(flow)
    wildcard = bytes(len(local))
    for key in ((_UDP, wildcard, port, peer, peer_port),
                (_UDP, local, port, wildcard, 0), (_UDP, wildcard, port, wildcard, 0)):
        if key in inodes:
            return inodes[key]
    return None


class _PacketTraffic:
    def __init__(self):
        # SOCK_DGRAM skips the data-link header. Copy at most the header prefix
        # and use MSG_TRUNC for the full observed IP packet size.
        self._socket = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(3))
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self._socket.settimeout(0.25)
        self.interfaces = _interface_ips()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._counts = {}
        self._stamp = time.monotonic()
        self.reader = threading.Thread(target=self._run, name="network-packets", daemon=True)
        self.reader.start()

    def _run(self):
        header = memoryview(bytearray(128))
        buffers = (header,)
        while not self._stop.is_set():
            try:
                length, _, _, address = self._socket.recvmsg_into(buffers, 0, socket.MSG_TRUNC)
            except TimeoutError:
                continue
            except OSError:
                break
            name, ethertype, kind = address[:3]
            ips = self.interfaces.get(name)
            if ips is None or kind == socket.PACKET_OTHERHOST or ethertype not in (_IPV4, _IPV6):
                continue
            outgoing = kind == socket.PACKET_OUTGOING
            flow = _packet_flow(header[:length], ethertype, outgoing, ips)
            if flow is None:
                continue
            with self._lock:
                # Bound a single interval even on a busy forwarding host.
                if len(self._counts) >= 16384 and flow not in self._counts:
                    continue
                counts = self._counts.setdefault(flow, [0, 0])
                counts[outgoing] += length

    def sample(self, pids):
        self.interfaces = _interface_ips()
        with self._lock:
            now = time.monotonic()
            flows, self._counts = self._counts, {}
            elapsed = now - self._stamp
            self._stamp = now
        protocols = {flow[0] for flow in flows}
        try:
            inodes = _flow_inodes(protocols) if protocols else {}
        except (OSError, struct.error):
            return {}, "無法讀取程序流量"
        matches = {flow: _flow_inode(flow, inodes) for flow in flows}
        used = {inode for inode in matches.values() if inode is not None}
        owners = _owners(used, pids) if used else {}
        rates = {}
        for flow, (received, sent) in flows.items():
            pid = owners.get(matches[flow])
            if pid is None:
                continue
            old_received, old_sent = rates.get(pid, (0, 0))
            rates[pid] = old_received + received, old_sent + sent
        return {pid: (recv / elapsed, sent / elapsed) for pid, (recv, sent) in rates.items()}, ""

    def close(self):
        self._stop.set()
        self.reader.join(timeout=1)
        self._socket.close()


class LinuxProcessNetwork:
    def __init__(self):
        try:
            self._capture = _PacketTraffic()
        except OSError:
            self._capture = None
        self._counters = {}
        self._stamp = None

    @property
    def scope(self):
        return "packet" if self._capture is not None else "tcp"

    @property
    def interfaces(self):
        return tuple(self._capture.interfaces) if self._capture is not None else None

    def close(self):
        if self._capture is not None:
            self._capture.close()
            self._capture = None

    def sample(self, pids):
        if self._capture is not None:
            if self._capture.reader.is_alive():
                return self._capture.sample(pids)
            self.close()
        try:
            counters = _tcp_counters()
        except (OSError, struct.error):
            self._counters, self._stamp = {}, None
            return {}, "無法讀取程序流量"
        now = time.monotonic()
        previous, stamp = self._counters, self._stamp
        self._counters, self._stamp = counters, now
        if stamp is None:
            return {}, "等待下一次取樣"

        # Short-lived connections that close between dumps leave no readable counters.
        active = {}
        for key, (received, sent) in counters.items():
            old_received, old_sent = previous.get(key, (0, 0))
            delta = max(0, received - old_received), max(0, sent - old_sent)
            if any(delta):
                active[key[0]] = delta
        if not active:
            return {}, ""
        rates = {}
        owners = _owners(active, pids)
        for inode, (received, sent) in active.items():
            pid = owners.get(inode)
            if pid is None:
                continue
            old_received, old_sent = rates.get(pid, (0, 0))
            rates[pid] = old_received + received, old_sent + sent
        elapsed = now - stamp
        return {pid: (recv / elapsed, sent / elapsed) for pid, (recv, sent) in rates.items()}, ""
