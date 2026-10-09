"""
Single ICMP echo probe -- supplemental reachability evidence for health classification.

Implementation: a Linux unprivileged ICMP datagram socket (socket.SOCK_DGRAM +
IPPROTO_ICMP, the mechanism `ping` itself uses without setuid). No subprocess, no shell,
no `ping` binary (the worker image has none), no extra capability (the worker runs as
UID 10001 with no added capabilities), no new dependency.

The kernel only allows these sockets for groups listed in net.ipv4.ping_group_range.
Docker sets "0 2147483647" for containers; Kubernetes treats that sysctl as "safe" and a
pod can set it via securityContext.sysctls. When it is not permitted, the probe returns
None ("could not test") -- never False -- and health falls back to TCP/22 evidence.

Result: True = echo reply received; False = no reply within the timeout (or an ICMP
error such as host unreachable); None = ICMP could not be attempted.
ICMP is supplemental only: some networks block it, so False alone never means "down".
"""

import errno
import logging
import os
import select
import socket
import struct
import time

logger = logging.getLogger("network_worker.health")

ICMP_TIMEOUT_SECONDS = float(os.getenv("HEALTH_ICMP_TIMEOUT_SECONDS", "1"))
ICMP_ENABLED = os.getenv("HEALTH_ICMP_ENABLED", "true").strip().lower() not in ("0", "false", "no")
ECHO_REQUEST, ECHO_REPLY = 8, 0

_warned = {"unavailable": False}


def _checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _packet(sequence):
    payload = b"network-platform-health"
    header = struct.pack("!BBHHH", ECHO_REQUEST, 0, 0, 0, sequence)
    checksum = _checksum(header + payload)
    # The kernel rewrites the identifier for datagram ICMP sockets (and fixes the checksum).
    return struct.pack("!BBHHH", ECHO_REQUEST, 0, checksum, 0, sequence) + payload


def icmp_available():
    """True if this process may open an unprivileged ICMP socket (diagnostics/validation)."""
    try:
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()
        return True
    except OSError:
        return False


def icmp_echo(host, timeout=None):
    """One echo request to an IPv4 address. True / False / None (could not test)."""
    if not ICMP_ENABLED:
        return None
    timeout = ICMP_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        address = socket.gethostbyname(str(host))  # management IPs are literal addresses
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EPROTONOSUPPORT, errno.EAFNOSUPPORT) or isinstance(exc, PermissionError):
            if not _warned["unavailable"]:
                _warned["unavailable"] = True
                logger.warning("icmp_probe_unavailable error=%s hint=set net.ipv4.ping_group_range for the worker pod; "
                               "health uses TCP/22 evidence only", type(exc).__name__)
            return None
        return False  # e.g. unresolvable host

    sequence = int(time.monotonic() * 1000) & 0xFFFF
    deadline = time.monotonic() + timeout
    try:
        sock.setblocking(False)
        sock.sendto(_packet(sequence), (address, 0))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                return False
            data, source = sock.recvfrom(1024)
            # Datagram ICMP sockets deliver the ICMP message without the IP header.
            if len(data) >= 8 and source[0] == address:
                kind, _, _, _, seq = struct.unpack("!BBHHH", data[:8])
                if kind == ECHO_REPLY and seq == sequence:
                    return True
    except OSError:
        return False  # host/network unreachable reported by the stack
    finally:
        sock.close()
