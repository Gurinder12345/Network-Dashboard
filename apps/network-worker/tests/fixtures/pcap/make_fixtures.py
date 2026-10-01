"""
Generate the small SYNTHETIC packet captures used by the PCAP analyzer tests.

    python3 tests/fixtures/pcap/make_fixtures.py        (from apps/network-worker)

Pure Python (struct only): Ethernet / IPv4 / TCP / UDP / ICMP with correct checksums,
minimal DNS messages and TLS 1.3 handshake records (no payload content of any kind).
Every packet is invented for a test scenario; nothing here was captured from a network.
Addresses are lab-private: client 10.1.1.10, server 10.2.2.20, resolver 10.3.3.53,
router 10.0.0.1. Output is deterministic, so regenerating must not change the files.
"""

import os
import struct

OUT = os.path.dirname(os.path.abspath(__file__))

CLIENT, SERVER, RESOLVER, ROUTER = "10.1.1.10", "10.2.2.20", "10.3.3.53", "10.0.0.1"
MAC_C, MAC_S = b"\x02\x00\x00\x00\x00\x0a", b"\x02\x00\x00\x00\x00\x14"
T0 = 1_780_000_000.0  # fixed epoch for deterministic files

FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10


def ip_bytes(addr):
    return bytes(int(part) for part in addr.split("."))


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def ipv4(src, dst, proto, payload, ident=0, df=True, ttl=64):
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, 0x4000 if df else 0, ttl, proto, 0,
                         ip_bytes(src), ip_bytes(dst))
    header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
    return header + payload


def ether(payload, src_mac=MAC_C, dst_mac=MAC_S):
    return dst_mac + src_mac + b"\x08\x00" + payload


def tcp(src, dst, sport, dport, seq, ack, flags, payload=b"", window=64240, mss=False):
    options = struct.pack("!BBH", 2, 4, 1460) if mss else b""
    offset = (20 + len(options)) // 4
    header = struct.pack("!HHIIBBHHH", sport, dport, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, offset << 4, flags, window, 0, 0) + options
    pseudo = ip_bytes(src) + ip_bytes(dst) + struct.pack("!BBH", 0, 6, len(header) + len(payload))
    csum = checksum(pseudo + header + payload)
    header = header[:16] + struct.pack("!H", csum) + header[18:]
    return ipv4(src, dst, 6, header + payload)


def udp(src, dst, sport, dport, payload):
    header = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0)
    pseudo = ip_bytes(src) + ip_bytes(dst) + struct.pack("!BBH", 0, 17, 8 + len(payload))
    csum = checksum(pseudo + header + payload) or 0xFFFF
    return ipv4(src, dst, 17, header[:6] + struct.pack("!H", csum) + payload)


def icmp(src, dst, icmp_type, code, rest, body):
    header = struct.pack("!BBH", icmp_type, code, 0) + rest
    csum = checksum(header + body)
    return ipv4(src, dst, 1, header[:2] + struct.pack("!H", csum) + header[4:] + body, df=False)


def dns(ident, name, response=False, rcode=0, answer=None):
    flags = (0x8000 | 0x0400 | 0x0100 | 0x0080 | rcode) if response else 0x0100
    question = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack("!HH", 1, 1)
    answers = b""
    if answer:
        answers = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 300, 4) + ip_bytes(answer)
    return struct.pack("!HHHHHH", ident, flags, 1, 1 if answer else 0, 0, 0) + question + answers


def tls_record(content_type, body, version=0x0303):
    return struct.pack("!BHH", content_type, version, len(body)) + body


def tls_handshake(msg_type, body):
    return struct.pack("!B", msg_type) + len(body).to_bytes(3, "big") + body


def client_hello(sni):
    name = sni.encode()
    sni_ext = struct.pack("!HH", 0, len(name) + 5) + struct.pack("!HBH", len(name) + 3, 0, len(name)) + name
    versions = struct.pack("!HHB", 43, 3, 2) + b"\x03\x04"
    extensions = sni_ext + versions
    body = b"\x03\x03" + bytes(range(32)) + b"\x00" + struct.pack("!H", 2) + b"\x13\x01" + b"\x01\x00" \
        + struct.pack("!H", len(extensions)) + extensions
    return tls_record(22, tls_handshake(1, body), version=0x0301)


def server_hello():
    extensions = struct.pack("!HH", 43, 2) + b"\x03\x04"
    body = b"\x03\x03" + bytes(range(32, 64)) + b"\x00" + b"\x13\x01" + b"\x00" + struct.pack("!H", len(extensions)) + extensions
    return tls_record(22, tls_handshake(2, body))


def tls_alert(description, level=2):
    return tls_record(21, bytes([level, description]))


# ---- writers -----------------------------------------------------------------------------

def write_pcap(name, packets):
    with open(os.path.join(OUT, name), "wb") as handle:
        handle.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for ts, frame in packets:
            sec = int(ts)
            usec = int(round((ts - sec) * 1_000_000))
            if usec == 1_000_000:
                sec, usec = sec + 1, 0
            handle.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)) + frame)


def write_pcapng(name, packets):
    def block(block_type, body):
        pad = (-len(body)) % 4
        total = 12 + len(body) + pad
        return struct.pack("<II", block_type, total) + body + b"\0" * pad + struct.pack("<I", total)

    with open(os.path.join(OUT, name), "wb") as handle:
        handle.write(block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)))
        handle.write(block(0x00000001, struct.pack("<HHI", 1, 0, 65535)))
        for ts, frame in packets:
            micros = int(round(ts * 1_000_000))
            body = struct.pack("<IIIII", 0, micros >> 32, micros & 0xFFFFFFFF, len(frame), len(frame))
            body += frame + b"\0" * ((-len(frame)) % 4)
            handle.write(block(0x00000006, body))


class Flow:
    """One TCP connection with automatic seq/ack bookkeeping (raw sequence numbers)."""

    def __init__(self, cport, sport=80, client=CLIENT, server=SERVER, cisn=1_000_000, sisn=5_000_000):
        self.cport, self.sport, self.client, self.server = cport, sport, client, server
        self.cseq, self.sseq = cisn, sisn

    def c(self, flags, payload=b"", seq=None, ack=None, window=64240, mss=False):
        frame = ether(tcp(self.client, self.server, self.cport, self.sport, self.cseq if seq is None else seq,
                          self.sseq if ack is None else ack, flags, payload, window, mss))
        if seq is None:
            self.cseq += len(payload) + (1 if flags & (SYN | FIN) else 0)
        return frame

    def s(self, flags, payload=b"", seq=None, ack=None, window=65160, mss=False):
        frame = ether(tcp(self.server, self.client, self.sport, self.cport, self.sseq if seq is None else seq,
                          self.cseq if ack is None else ack, flags, payload, window, mss), MAC_S, MAC_C)
        if seq is None:
            self.sseq += len(payload) + (1 if flags & (SYN | FIN) else 0)
        return frame


REQUEST = b"GET /status HTTP/1.1\r\nHost: app.example.lab\r\n\r\n"


def body(size):
    return bytes(size)  # zero-filled: no content of any kind


def handshake(flow, t, rtt):
    return [(t, flow.c(SYN, mss=True)), (t + rtt, flow.s(SYN | ACK, mss=True)), (t + rtt + 0.0002, flow.c(ACK))]


def closing(flow, t, rtt):
    return [(t, flow.c(FIN | ACK)), (t + rtt, flow.s(FIN | ACK)), (t + rtt + 0.0002, flow.c(ACK))]


# ---- scenarios -------------------------------------------------------------------------

def tcp_normal():
    f = Flow(50000)
    p = handshake(f, T0, 0.020)
    t = T0 + 0.021
    p += [(t, f.c(PSH | ACK, REQUEST)), (t + 0.020, f.s(ACK)), (t + 0.035, f.s(PSH | ACK, body(1200))),
          (t + 0.036, f.c(ACK))]
    p += closing(f, t + 0.100, 0.020)
    return p


def tcp_retransmission():
    f = Flow(50001)
    p = handshake(f, T0, 0.010)
    t = T0 + 0.011
    p.append((t, f.c(PSH | ACK, REQUEST)))
    t += 0.012
    seg = [f.sseq + i * 1000 for i in range(5)]
    f.sseq = seg[0]
    sent = []
    for i in range(5):  # server sends 5 segments; the 2nd is lost before the client
        frame = f.s(ACK, body(1000))
        if i != 1:
            sent.append((t + i * 0.001, frame))
    p += sent
    ack_lost = seg[1]
    for i in range(3):  # client: one ACK for seg 1, then duplicate ACKs for the missing seg 2
        p.append((t + 0.0105 + i * 0.001, f.c(ACK, ack=ack_lost)))
    p.append((t + 0.0125, f.c(ACK, ack=ack_lost)))
    p.append((t + 0.015, f.s(ACK, body(1000), seq=seg[1])))           # fast retransmission
    p.append((t + 0.0251, f.c(ACK, ack=f.sseq)))
    p.append((t + 0.030, f.c(PSH | ACK, b"x" * 0 + REQUEST)))           # second request
    p.append((t + 0.330, f.c(PSH | ACK, REQUEST, seq=f.cseq - len(REQUEST))))  # RTO retransmission
    p.append((t + 0.340, f.s(ACK)))
    p += closing(f, t + 0.400, 0.010)
    return p


def tcp_reset():
    f = Flow(50002)
    p = handshake(f, T0, 0.015)
    t = T0 + 0.016
    p += [(t, f.c(PSH | ACK, REQUEST)), (t + 0.016, f.s(RST | ACK))]
    return p


def tcp_syn_no_answer():
    f = Flow(50003, sport=8443)
    frame = f.c(SYN, mss=True)
    return [(T0, frame), (T0 + 1.0, frame), (T0 + 3.0, frame)]


def tcp_zero_window():
    f = Flow(50004)
    p = handshake(f, T0, 0.010)
    t = T0 + 0.011
    p.append((t, f.c(PSH | ACK, REQUEST)))
    t += 0.011
    p.append((t, f.s(ACK, body(1000))))
    p.append((t + 0.0005, f.c(ACK, window=0)))                        # receiver window full
    p.append((t + 0.200, f.s(ACK, body(1), seq=f.sseq)))              # zero-window probe
    p.append((t + 0.2005, f.c(ACK, window=0)))
    p.append((t + 0.600, f.c(ACK, window=64240)))                     # window update
    p.append((t + 0.601, f.s(ACK, body(1000))))
    p.append((t + 0.602, f.c(ACK)))
    p += closing(f, t + 0.700, 0.010)
    return p


def app_delay(prefix=""):
    """Handshake + request fine; the server answers 1.8 s later. No transport problems."""
    f = Flow(50005)
    p = handshake(f, T0, 0.018)
    t = T0 + 0.019
    p += [(t, f.c(PSH | ACK, REQUEST)), (t + 0.018, f.s(ACK)), (t + 1.818, f.s(PSH | ACK, body(800))),
          (t + 1.819, f.c(ACK))]
    p += closing(f, t + 1.900, 0.018)
    return p


def dns_ok():
    q = ether(udp(CLIENT, RESOLVER, 53001, 53, dns(0x1111, "app.example.lab")))
    r = ether(udp(RESOLVER, CLIENT, 53, 53001, dns(0x1111, "app.example.lab", True, 0, "10.2.2.20")), MAC_S, MAC_C)
    return [(T0, q), (T0 + 0.004, r)]


def dns_problems():
    p = []
    q = lambda i, n, port: ether(udp(CLIENT, RESOLVER, port, 53, dns(i, n)))
    r = lambda i, n, port, rc, a=None: ether(udp(RESOLVER, CLIENT, 53, port, dns(i, n, True, rc, a)), MAC_S, MAC_C)
    p += [(T0, q(0x2001, "nosuchhost.example.lab", 53101)), (T0 + 0.015, r(0x2001, "nosuchhost.example.lab", 53101, 3))]
    p += [(T0 + 0.100, q(0x2002, "slow.example.lab", 53102)), (T0 + 1.100, q(0x2002, "slow.example.lab", 53102)),
          (T0 + 1.350, r(0x2002, "slow.example.lab", 53102, 0, "10.2.2.30"))]
    p += [(T0 + 1.500, q(0x2003, "broken.example.lab", 53103)), (T0 + 1.520, r(0x2003, "broken.example.lab", 53103, 2))]
    p += [(T0 + 2.000, q(0x2004, "lost.example.lab", 53104))]  # never answered
    return p


def tls_sessions():
    ok, bad = Flow(50010, sport=443), Flow(50011, sport=443, cisn=2_000_000, sisn=7_000_000)
    p = handshake(ok, T0, 0.012)
    p += [(T0 + 0.013, ok.c(PSH | ACK, client_hello("app.example.lab"))),
          (T0 + 0.027, ok.s(PSH | ACK, server_hello())), (T0 + 0.028, ok.c(ACK))]
    t = T0 + 0.200
    p += handshake(bad, t, 0.012)
    p += [(t + 0.013, bad.c(PSH | ACK, client_hello("legacy.example.lab"))),
          (t + 0.026, bad.s(PSH | ACK, tls_alert(40))), (t + 0.027, bad.s(RST | ACK))]
    return sorted(p, key=lambda x: x[0])


def icmp_frag_needed():
    f = Flow(50020)
    p = handshake(f, T0, 0.010)
    t = T0 + 0.011
    big = f.c(PSH | ACK, body(1460))
    p.append((t, big))
    original = big[14:14 + 28]  # original IP header + first 8 bytes of TCP
    p.append((t + 0.003, ether(icmp(ROUTER, CLIENT, 3, 4, struct.pack("!HH", 0, 1400), original), MAC_S, MAC_C)))
    p.append((t + 0.004, ether(icmp(ROUTER, CLIENT, 11, 0, b"\0\0\0\0", original), MAC_S, MAC_C)))
    return p


# ---- dual capture: the same connection seen at the client and at the server ---------------

SERVER_CLOCK_OFFSET = 5.000   # server clock runs 5 s ahead of the client clock
ONE_WAY = 0.010               # 10 ms each way between the two capture points


def dual(lose_forward=False, lose_reverse=False, server_delay=0.002):
    """
    Build both captures from one timeline. Each packet is seen at its sender's capture
    point at send time and at the receiver's capture point ONE_WAY later (server clock
    shifted by SERVER_CLOCK_OFFSET), unless it is lost between the capture points.
    """
    f = Flow(51000)
    client_side, server_side = [], []

    def c2s(t, frame, lost=False):
        client_side.append((t, frame))
        if not lost:
            server_side.append((t + ONE_WAY + SERVER_CLOCK_OFFSET, frame))
        return t + ONE_WAY

    def s2c(t_server_local, frame, lost=False):
        server_side.append((t_server_local + SERVER_CLOCK_OFFSET, frame))
        if not lost:
            client_side.append((t_server_local + ONE_WAY, frame))
        return t_server_local + ONE_WAY

    t = T0
    arrive = c2s(t, f.c(SYN, mss=True))
    back = s2c(arrive + 0.0005, f.s(SYN | ACK, mss=True))
    arrive = c2s(back + 0.0002, f.c(ACK))
    t = back + 0.001
    first_request_seq = f.cseq
    arrive = c2s(t, f.c(PSH | ACK, REQUEST), lost=lose_forward)
    if lose_forward:
        # Client retransmits after 200 ms; only the retransmission reaches the server.
        t += 0.200
        arrive = c2s(t, f.c(PSH | ACK, REQUEST, seq=first_request_seq))
    s2c(arrive + 0.0003, f.s(ACK))
    respond_at = arrive + server_delay
    seg1 = f.s(ACK, body(1000))
    seg2 = f.s(PSH | ACK, body(600))
    back1 = s2c(respond_at, seg1)
    back2 = s2c(respond_at + 0.0005, seg2, lost=lose_reverse)
    if lose_reverse:
        # Client never saw seg2: duplicate ACK, server retransmits seg2 later.
        c2s(back1 + 0.0002, f.c(ACK, ack=f.sseq - 600))
        back2 = s2c(respond_at + 0.250, f.s(PSH | ACK, body(600), seq=f.sseq - 600))
    arrive = c2s(back2 + 0.0002, f.c(ACK))
    t = back2 + 0.050
    arrive = c2s(t, f.c(FIN | ACK))
    back = s2c(arrive + 0.0005, f.s(FIN | ACK))
    c2s(back + 0.0002, f.c(ACK))
    return sorted(client_side, key=lambda x: x[0]), sorted(server_side, key=lambda x: x[0])


def main():
    write_pcap("tcp_normal.pcap", tcp_normal())
    write_pcapng("tcp_normal.pcapng", tcp_normal())
    write_pcap("tcp_retransmission.pcap", tcp_retransmission())
    write_pcap("tcp_reset.pcap", tcp_reset())
    write_pcap("tcp_syn_no_answer.pcap", tcp_syn_no_answer())
    write_pcap("tcp_zero_window.pcap", tcp_zero_window())
    write_pcap("app_delay.pcap", app_delay())
    write_pcap("dns_ok.pcap", dns_ok())
    write_pcap("dns_problems.pcap", dns_problems())
    write_pcap("tls_sessions.pcap", tls_sessions())
    write_pcap("icmp_frag_needed.pcap", icmp_frag_needed())
    for name, kwargs in (("dual_delay", {"server_delay": 1.800}), ("dual_loss", {"lose_forward": True, "lose_reverse": True})):
        client, server = dual(**kwargs)
        write_pcap(f"{name}_client.pcap", client)
        write_pcap(f"{name}_server.pcap", server)
    with open(os.path.join(OUT, "not_a_capture.pcap"), "wb") as handle:
        handle.write(b"this is plain text, not a packet capture\n")
    with open(os.path.join(OUT, "truncated.pcap"), "wb") as handle:
        frames = tcp_normal()
        ts, frame = frames[0]
        handle.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
                     + struct.pack("<IIII", int(ts), 0, 9000, 9000) + frame[:20])


if __name__ == "__main__":
    main()
