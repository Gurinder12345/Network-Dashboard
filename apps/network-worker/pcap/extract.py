"""
One streaming TShark pass per capture -> per-flow / DNS / TLS / ICMP observations.

Only protocol metadata is requested from TShark (addresses, ports, TCP flags/sequence
numbers/analysis flags, DNS header + query name, TLS handshake types/versions/SNI/alerts,
ICMP type/code/MTU). No payload bytes, HTTP fields, cookies or credentials are ever
extracted. Memory is bounded: per-stream counters, not per-packet records (per-segment
identities are kept only for dual-capture correlation, up to a cap).
"""

import os
from statistics import median

from pcap.tshark import TsharkError, iter_fields

MAX_PACKETS = int(os.getenv("PCAP_MAX_PACKETS", "5000000"))
MAX_TCP_STREAMS = int(os.getenv("PCAP_MAX_TCP_STREAMS", "100000"))
MAX_CORRELATION_SEGMENTS = int(os.getenv("PCAP_MAX_CORRELATION_SEGMENTS", "1000000"))
MAX_DNS_TRANSACTIONS = 20000
MAX_ICMP_EVENTS = 500

FIELDS = (
    "frame.number", "frame.time_epoch", "frame.len",
    "ip.src", "ip.dst", "ipv6.src", "ipv6.dst", "ip.proto",
    "tcp.stream", "tcp.srcport", "tcp.dstport", "tcp.flags", "tcp.seq_raw", "tcp.ack_raw", "tcp.len",
    "tcp.window_size",
    "tcp.analysis.retransmission", "tcp.analysis.fast_retransmission", "tcp.analysis.spurious_retransmission",
    "tcp.analysis.duplicate_ack", "tcp.analysis.out_of_order", "tcp.analysis.lost_segment",
    "tcp.analysis.ack_lost_segment", "tcp.analysis.zero_window", "tcp.analysis.zero_window_probe",
    "tcp.analysis.window_full", "tcp.analysis.keep_alive", "tcp.analysis.keep_alive_ack", "tcp.analysis.ack_rtt",
    "udp.stream", "udp.srcport", "udp.dstport",
    "dns.id", "dns.flags.response", "dns.flags.rcode", "dns.qry.name", "dns.qry.type", "dns.time",
    "dns.retransmission", "dns.response_to",
    "tls.handshake.type", "tls.handshake.version", "tls.handshake.extensions.supported_version",
    "tls.handshake.extensions_server_name", "tls.alert_message.level", "tls.alert_message.desc",
    "icmp.type", "icmp.code", "icmp.mtu", "icmpv6.type", "icmpv6.code", "icmpv6.mtu",
)
F = {name: i for i, name in enumerate(FIELDS)}

SYN, ACK, RST, FIN = 0x02, 0x10, 0x04, 0x01
FLAG_KEYS = (
    ("retransmission", "tcp.analysis.retransmission"),
    ("fast_retransmission", "tcp.analysis.fast_retransmission"),
    ("spurious_retransmission", "tcp.analysis.spurious_retransmission"),
    ("duplicate_ack", "tcp.analysis.duplicate_ack"),
    ("out_of_order", "tcp.analysis.out_of_order"),
    ("lost_segment", "tcp.analysis.lost_segment"),
    ("ack_lost_segment", "tcp.analysis.ack_lost_segment"),
    ("zero_window", "tcp.analysis.zero_window"),
    ("zero_window_probe", "tcp.analysis.zero_window_probe"),
    ("window_full", "tcp.analysis.window_full"),
    ("keep_alive", "tcp.analysis.keep_alive"),
    ("keep_alive_ack", "tcp.analysis.keep_alive_ack"),
)

TLS_VERSIONS = {"0x0300": "SSL 3.0", "0x0301": "TLS 1.0", "0x0302": "TLS 1.1", "0x0303": "TLS 1.2", "0x0304": "TLS 1.3"}
TLS_ALERTS = {
    0: "close_notify", 10: "unexpected_message", 20: "bad_record_mac", 22: "record_overflow", 40: "handshake_failure",
    42: "bad_certificate", 43: "unsupported_certificate", 44: "certificate_revoked", 45: "certificate_expired",
    46: "certificate_unknown", 47: "illegal_parameter", 48: "unknown_ca", 49: "access_denied", 50: "decode_error",
    51: "decrypt_error", 70: "protocol_version", 71: "insufficient_security", 80: "internal_error",
    86: "inappropriate_fallback", 90: "user_canceled", 109: "missing_extension", 110: "unsupported_extension",
    112: "unrecognized_name", 113: "bad_certificate_status_response", 116: "certificate_required",
    120: "no_application_protocol",
}
DNS_RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
DNS_QTYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV", 65: "HTTPS"}


def _first(value):
    return value.split(",", 1)[0] if value else ""


def _int(value, base=10):
    try:
        return int(value, base)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DirStats:
    __slots__ = ("packets", "bytes", "payload_bytes", "flags", "first_payload_ts", "other_last_payload_before",
                 "retrans_before_first_payload", "last_payload_ts")

    def __init__(self):
        self.packets = self.bytes = self.payload_bytes = 0
        self.flags = {key: 0 for key, _ in FLAG_KEYS}
        self.first_payload_ts = self.other_last_payload_before = self.last_payload_ts = None
        self.retrans_before_first_payload = None


class TcpStream:
    __slots__ = ("stream", "a", "b", "first_ts", "last_ts", "dirs", "syn_times", "syn_src", "synack_ts", "synack_src",
                 "ack_ts", "rsts", "fins", "ack_rtt", "retrans_total", "tls", "frames")

    def __init__(self, stream, a, b, ts):
        self.stream, self.a, self.b = stream, a, b
        self.first_ts = self.last_ts = ts
        self.dirs = {a: DirStats(), b: DirStats()}
        self.syn_times, self.syn_src = [], None
        self.synack_ts = self.synack_src = self.ack_ts = None
        self.rsts, self.fins, self.ack_rtt = [], [], []
        self.retrans_total = 0
        self.tls = None
        self.frames = 0

    def other(self, ep):
        return self.b if ep == self.a else self.a


def _endpoint_add(table, addr, frame_len, sent):
    entry = table.setdefault(addr, [0, 0, 0, 0])  # tx packets, tx bytes, rx packets, rx bytes
    if sent:
        entry[0] += 1
        entry[1] += frame_len
    else:
        entry[2] += 1
        entry[3] += frame_len


def extract(path, collect_segments=False):
    streams = {}
    udp = {}
    endpoints = {}
    dns_queries, dns_by_frame, dns_responses = [], {}, []
    icmp_events, icmp_counts = [], {}
    segments = [] if collect_segments else None
    limits = {"tcp_streams_untracked": 0, "segments_truncated": False, "dns_truncated": False, "icmp_truncated": False}
    packets = total_bytes = 0
    first_ts = last_ts = None

    for v in iter_fields(path, FIELDS):
        packets += 1
        if packets > MAX_PACKETS:
            raise TsharkError(f"Capture exceeds the {MAX_PACKETS:,}-packet analysis limit")

        ts = _float(v[F["frame.time_epoch"]])
        flen = _int(v[F["frame.len"]]) or 0
        if ts is None:
            continue
        first_ts = ts if first_ts is None else min(first_ts, ts)
        last_ts = ts if last_ts is None else max(last_ts, ts)
        total_bytes += flen

        src = _first(v[F["ip.src"]]) or _first(v[F["ipv6.src"]])
        dst = _first(v[F["ip.dst"]]) or _first(v[F["ipv6.dst"]])
        if src:
            _endpoint_add(endpoints, src, flen, True)
        if dst:
            _endpoint_add(endpoints, dst, flen, False)

        icmp_type = _first(v[F["icmp.type"]]) or _first(v[F["icmpv6.type"]])
        if icmp_type:
            family = "icmpv6" if not v[F["icmp.type"]] else "icmp"
            code = _int(_first(v[F[f"{family}.code"]]))
            key = (family, _int(icmp_type), code)
            icmp_counts[key] = icmp_counts.get(key, 0) + 1
            if len(icmp_events) < MAX_ICMP_EVENTS:
                inner_dst = v[F["ip.dst"]].split(",")[1] if "," in v[F["ip.dst"]] else None
                inner_proto = v[F["ip.proto"]].split(",")[1] if "," in v[F["ip.proto"]] else None
                icmp_events.append({
                    "frame": _int(v[F["frame.number"]]), "ts": ts, "family": family, "type": _int(icmp_type),
                    "code": code, "src": src, "dst": dst, "mtu": _int(_first(v[F[f"{family}.mtu"]])),
                    "original_dst": inner_dst, "original_protocol": {"6": "tcp", "17": "udp", "1": "icmp"}.get(inner_proto, inner_proto),
                    "original_dst_port": _int(_first(v[F["tcp.dstport"]])) or _int(_first(v[F["udp.dstport"]])),
                })
            else:
                limits["icmp_truncated"] = True
            continue  # embedded headers inside ICMP must not be counted as TCP/UDP traffic

        tcp_stream = _int(_first(v[F["tcp.stream"]]))
        if tcp_stream is not None:
            sport, dport = _int(_first(v[F["tcp.srcport"]])), _int(_first(v[F["tcp.dstport"]]))
            ep, peer = (src, sport), (dst, dport)
            s = streams.get(tcp_stream)
            if s is None:
                if len(streams) >= MAX_TCP_STREAMS:
                    limits["tcp_streams_untracked"] += 1
                    continue
                s = streams[tcp_stream] = TcpStream(tcp_stream, ep, peer, ts)
            if ep not in s.dirs:  # defensive: a reused stream index with different endpoints
                continue
            s.last_ts = ts
            s.frames += 1
            flags = _int(_first(v[F["tcp.flags"]]), 16) or 0
            tcp_len = _int(_first(v[F["tcp.len"]])) or 0
            d = s.dirs[ep]
            d.packets += 1
            d.bytes += flen
            d.payload_bytes += tcp_len

            set_flags = {key: bool(v[F[name]]) for key, name in FLAG_KEYS}
            for key, present in set_flags.items():
                if present:
                    d.flags[key] += 1
            retrans = set_flags["retransmission"] or set_flags["fast_retransmission"] or set_flags["spurious_retransmission"]
            if retrans:
                s.retrans_total += 1

            if flags & SYN and not flags & ACK:
                s.syn_times.append(ts)
                s.syn_src = ep
            elif flags & SYN and flags & ACK:
                if s.synack_ts is None:
                    s.synack_ts, s.synack_src = ts, ep
            elif flags & ACK and s.synack_ts is not None and s.ack_ts is None and ep == s.other(s.synack_src):
                s.ack_ts = ts
            if flags & RST:
                s.rsts.append((ts, ep, tcp_len))
            if flags & FIN:
                s.fins.append((ts, ep))

            rtt = _float(_first(v[F["tcp.analysis.ack_rtt"]]))
            if rtt is not None and len(s.ack_rtt) < 5000:
                s.ack_rtt.append(rtt)

            if tcp_len > 0 and not retrans and not set_flags["zero_window_probe"] and not set_flags["keep_alive"]:
                if d.first_payload_ts is None:
                    d.first_payload_ts = ts
                    d.other_last_payload_before = s.dirs[s.other(ep)].last_payload_ts
                    d.retrans_before_first_payload = s.retrans_total
                d.last_payload_ts = ts

            hs_types = [t for t in v[F["tls.handshake.type"]].split(",") if t]
            alert_desc = [x for x in v[F["tls.alert_message.desc"]].split(",") if x]
            if hs_types or alert_desc:
                tls = s.tls or {"client_hello_ts": None, "server_hello_ts": None, "sni": None, "offered_versions": [],
                                "negotiated_version": None, "handshake_types": [], "alerts": [], "client_hello_src": None}
                s.tls = tls
                types = [_int(t) for t in hs_types]
                for t in types:
                    if t is not None and t not in tls["handshake_types"]:
                        tls["handshake_types"].append(t)
                supported = [x for x in v[F["tls.handshake.extensions.supported_version"]].split(",") if x]
                if 1 in types and tls["client_hello_ts"] is None:
                    tls["client_hello_ts"], tls["client_hello_src"] = ts, ep
                    tls["sni"] = _first(v[F["tls.handshake.extensions_server_name"]]) or None
                    offered = supported or [_first(v[F["tls.handshake.version"]])]
                    tls["offered_versions"] = [TLS_VERSIONS.get(x, x) for x in offered if x]
                if 2 in types and tls["server_hello_ts"] is None:
                    tls["server_hello_ts"] = ts
                    version = _first(v[F["tls.handshake.extensions.supported_version"]]) or _first(v[F["tls.handshake.version"]])
                    tls["negotiated_version"] = TLS_VERSIONS.get(version, version) or None
                levels = v[F["tls.alert_message.level"]].split(",")
                for i, desc in enumerate(alert_desc):
                    code = _int(desc)
                    level = _int(levels[i]) if i < len(levels) else None
                    tls["alerts"].append({"ts": ts, "from": ep, "level": "fatal" if level == 2 else "warning" if level == 1 else None,
                                          "code": code, "description": TLS_ALERTS.get(code, f"alert {code}")})

            if segments is not None:
                if len(segments) < MAX_CORRELATION_SEGMENTS:
                    segments.append((tcp_stream, ep, _int(_first(v[F["tcp.seq_raw"]])), _int(_first(v[F["tcp.ack_raw"]])),
                                     tcp_len, flags & (SYN | ACK | RST | FIN), ts))
                else:
                    limits["segments_truncated"] = True
            continue

        udp_stream = _int(_first(v[F["udp.stream"]]))
        if udp_stream is not None:
            sport, dport = _int(_first(v[F["udp.srcport"]])), _int(_first(v[F["udp.dstport"]]))
            ep, peer = (src, sport), (dst, dport)
            u = udp.get(udp_stream)
            if u is None:
                u = udp[udp_stream] = {"stream": udp_stream, "client": ep, "server": peer, "first_ts": ts, "last_ts": ts,
                                       "dirs": {ep: [0, 0], peer: [0, 0]}, "dns": False}
            u["last_ts"] = ts
            if ep in u["dirs"]:
                u["dirs"][ep][0] += 1
                u["dirs"][ep][1] += flen

            dns_id = _first(v[F["dns.id"]])
            if dns_id:
                u["dns"] = True
                frame = _int(v[F["frame.number"]])
                name = _first(v[F["dns.qry.name"]]) or None
                qtype = _int(_first(v[F["dns.qry.type"]]))
                if _first(v[F["dns.flags.response"]]) in ("True", "1"):
                    dns_responses.append({"frame": frame, "ts": ts, "id": dns_id, "name": name,
                                          "rcode": _int(_first(v[F["dns.flags.rcode"]])),
                                          "latency": _float(_first(v[F["dns.time"]])),
                                          "response_to": _int(_first(v[F["dns.response_to"]])), "client": peer, "server": ep})
                elif len(dns_queries) < MAX_DNS_TRANSACTIONS:
                    query = {"frame": frame, "ts": ts, "id": dns_id, "name": name,
                             "type": DNS_QTYPES.get(qtype, str(qtype) if qtype is not None else None),
                             "client": ep, "server": peer, "udp_stream": udp_stream,
                             "retransmission": _first(v[F["dns.retransmission"]]) in ("True", "1")}
                    dns_queries.append(query)
                    dns_by_frame[frame] = query
                else:
                    limits["dns_truncated"] = True

    if packets == 0:
        raise TsharkError("Capture contains no packets")

    return {
        "packets": packets, "bytes": total_bytes, "first_ts": first_ts, "last_ts": last_ts,
        "tcp": streams, "udp": udp, "endpoints": endpoints,
        "dns": _dns_transactions(dns_queries, dns_by_frame, dns_responses, last_ts),
        "icmp": {"events": icmp_events, "counts": icmp_counts},
        "segments": segments, "limits": limits,
    }


def _dns_transactions(queries, by_frame, responses, capture_end):
    """Pair queries with responses (tshark's response_to), counting repeated queries."""
    transactions = []
    originals = {}
    for q in queries:
        key = (q["id"], q["name"], q["client"], q["server"])
        if q["retransmission"] and key in originals:
            originals[key]["repeats"] += 1
            continue
        t = {"query_frame": q["frame"], "ts": q["ts"], "id": q["id"], "name": q["name"], "type": q["type"],
             "client": q["client"], "server": q["server"], "repeats": 0, "rcode": None, "rcode_name": None,
             "latency": None, "answered": False, "capture_after_query_s": round(capture_end - q["ts"], 3)}
        originals[key] = t
        transactions.append(t)
    by_original = {t["query_frame"]: t for t in transactions}
    for r in responses:
        target = by_original.get(r["response_to"])
        if target is None and r["response_to"] in by_frame:  # answer to a repeated query
            q = by_frame[r["response_to"]]
            target = originals.get((q["id"], q["name"], q["client"], q["server"]))
        if target is None or target["answered"]:
            continue
        target.update(answered=True, rcode=r["rcode"], rcode_name=DNS_RCODES.get(r["rcode"], str(r["rcode"])),
                      latency=r["latency"] if r["latency"] is not None else round(r["ts"] - target["ts"], 6))
    return transactions


def summarize_rtt(samples):
    if not samples:
        return None
    ordered = sorted(samples)
    return {"count": len(ordered), "min_ms": round(ordered[0] * 1000, 3), "median_ms": round(median(ordered) * 1000, 3),
            "max_ms": round(ordered[-1] * 1000, 3)}
