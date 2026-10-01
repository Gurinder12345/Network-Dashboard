"""
Turn one capture's extraction into JSON-ready observations and derived metrics:
capture summary, endpoints, TCP/UDP flows, DNS transactions, TLS sessions, ICMP events.

Client/server inference never assumes "lower address = client":
    1. sender of the SYN                     (client_inferred_by = "syn")
    2. receiver of the SYN/ACK               ("syn_ack")
    3. otherwise the side using the higher (ephemeral-looking) port, labelled
       "port_heuristic" so the UI and rules know it is a guess.
"""

from pcap.extract import summarize_rtt

MAX_FLOWS = 500
MAX_ENDPOINTS = 100
MAX_DNS_ROWS = 300


def ms(seconds):
    return None if seconds is None else round(seconds * 1000, 3)


def endpoint(ep):
    return {"ip": ep[0], "port": ep[1]}


def label(ep):
    return f"[{ep[0]}]:{ep[1]}" if ":" in (ep[0] or "") else f"{ep[0]}:{ep[1]}"


def infer_client(s):
    if s.syn_src is not None:
        return s.syn_src, "syn"
    if s.synack_src is not None:
        return s.other(s.synack_src), "syn_ack"
    a_port, b_port = s.a[1] or 0, s.b[1] or 0
    return (s.a if a_port >= b_port else s.b), "port_heuristic"


def handshake_state(s, client, server):
    if s.syn_times and s.synack_ts is None:
        refused = [r for r in s.rsts if r[1] == server]
        return "refused" if refused else "no_syn_ack"
    if s.synack_ts is not None and s.ack_ts is None:
        return "no_final_ack"
    if s.syn_times and s.synack_ts is not None:
        return "complete"
    return "not_observed"


def tcp_flow(s, t0):
    client, how = infer_client(s)
    server = s.other(client)
    c, v = s.dirs[client], s.dirs[server]
    state = handshake_state(s, client, server)
    syn_to_synack = (s.synack_ts - s.syn_times[0]) if s.syn_times and s.synack_ts is not None else None
    synack_to_ack = (s.ack_ts - s.synack_ts) if s.ack_ts is not None and s.synack_ts is not None else None

    # Request -> first response: the client spoke first and the server answered.
    response_delay = None
    if v.first_payload_ts is not None and v.other_last_payload_before is not None:
        response_delay = v.first_payload_ts - v.other_last_payload_before
    server_first = v.first_payload_ts is not None and (c.first_payload_ts is None or v.first_payload_ts < c.first_payload_ts)

    def side(ep):
        return "client" if ep == client else "server"

    flags = {key: c.flags[key] + v.flags[key] for key in c.flags}
    flow = {
        "id": f"tcp-{s.stream}",
        "protocol": "tcp",
        "stream": s.stream,
        "client": endpoint(client),
        "server": endpoint(server),
        "client_inferred_by": how,
        "key": f"tcp {label(client)} -> {label(server)}",
        "start_s": round(s.first_ts - t0, 6),
        "duration_s": round(s.last_ts - s.first_ts, 6),
        "packets": c.packets + v.packets,
        "bytes": c.bytes + v.bytes,
        "client_to_server": {"packets": c.packets, "bytes": c.bytes, "payload_bytes": c.payload_bytes},
        "server_to_client": {"packets": v.packets, "bytes": v.bytes, "payload_bytes": v.payload_bytes},
        "handshake": {
            "state": state,
            "syn_packets": len(s.syn_times),
            "syn_to_synack_ms": ms(syn_to_synack),
            "synack_to_ack_ms": ms(synack_to_ack),
            "rtt_ms": ms(syn_to_synack + synack_to_ack) if syn_to_synack is not None and synack_to_ack is not None else None,
        },
        "tcp": {
            "retransmissions": flags["retransmission"],
            "fast_retransmissions": flags["fast_retransmission"],
            "spurious_retransmissions": flags["spurious_retransmission"],
            "duplicate_acks": flags["duplicate_ack"],
            "out_of_order": flags["out_of_order"],
            "lost_segments": flags["lost_segment"],
            "ack_lost_segments": flags["ack_lost_segment"],
            "zero_window_from_client": c.flags["zero_window"],
            "zero_window_from_server": v.flags["zero_window"],
            "zero_window_probes": flags["zero_window_probe"],
            "window_full": flags["window_full"],
            "keep_alives": flags["keep_alive"],
            "keep_alive_acks": flags["keep_alive_ack"],
            "retransmissions_by": {"client": c.flags["retransmission"], "server": v.flags["retransmission"]},
            "resets": [{"at_s": round(ts - t0, 6), "from": side(ep)} for ts, ep, _ in s.rsts[:10]],
            "reset_count": len(s.rsts),
            "fins": [{"at_s": round(ts - t0, 6), "from": side(ep)} for ts, ep in s.fins[:4]],
        },
        "rtt": summarize_rtt(s.ack_rtt),
        "timing": {
            "request_to_first_response_ms": ms(response_delay),
            "first_request_at_s": round(c.first_payload_ts - t0, 6) if c.first_payload_ts is not None else None,
            "first_response_at_s": round(v.first_payload_ts - t0, 6) if v.first_payload_ts is not None else None,
            "server_spoke_first": server_first,
            "retransmissions_before_response": v.retrans_before_first_payload,
        },
        "tls": tls_session(s, client, t0) if s.tls else None,
        "issues": [],
    }
    return flow


def tls_session(s, client, t0):
    tls = s.tls
    handshake_ms = None
    if tls["client_hello_ts"] is not None and tls["server_hello_ts"] is not None:
        handshake_ms = ms(tls["server_hello_ts"] - tls["client_hello_ts"])
    return {
        "sni": tls["sni"],
        "offered_versions": tls["offered_versions"],
        "negotiated_version": tls["negotiated_version"],
        "client_hello_at_s": round(tls["client_hello_ts"] - t0, 6) if tls["client_hello_ts"] is not None else None,
        "server_hello_at_s": round(tls["server_hello_ts"] - t0, 6) if tls["server_hello_ts"] is not None else None,
        "client_hello_to_server_hello_ms": handshake_ms,
        "handshake_messages": tls["handshake_types"],
        "alerts": [{"at_s": round(a["ts"] - t0, 6), "from": "client" if a["from"] == client else "server",
                    "level": a["level"], "code": a["code"], "description": a["description"]} for a in tls["alerts"][:10]],
    }


def udp_flow(u, t0, dns_count):
    client, server = u["client"], u["server"]
    c, v = u["dirs"][client], u["dirs"][server]
    return {
        "id": f"udp-{u['stream']}",
        "protocol": "udp",
        "stream": u["stream"],
        "client": endpoint(client),
        "server": endpoint(server),
        "client_inferred_by": "first_packet",
        "key": f"udp {label(client)} -> {label(server)}",
        "start_s": round(u["first_ts"] - t0, 6),
        "duration_s": round(u["last_ts"] - u["first_ts"], 6),
        "packets": c[0] + v[0],
        "bytes": c[1] + v[1],
        "client_to_server": {"packets": c[0], "bytes": c[1]},
        "server_to_client": {"packets": v[0], "bytes": v[1]},
        "dns_transactions": dns_count,
        "issues": [],
    }


def dns_rows(transactions, t0):
    rows = []
    for t in transactions[:MAX_DNS_ROWS]:
        rows.append({
            "at_s": round(t["ts"] - t0, 6), "id": t["id"], "name": t["name"], "type": t["type"],
            "client": label(t["client"]), "server": label(t["server"]), "answered": t["answered"],
            "rcode": t["rcode_name"], "latency_ms": ms(t["latency"]), "repeats": t["repeats"],
            "capture_after_query_s": t["capture_after_query_s"],
        })
    return rows


ICMP_NAMES = {
    ("icmp", 3): "destination unreachable", ("icmp", 11): "time exceeded", ("icmp", 5): "redirect",
    ("icmpv6", 1): "destination unreachable", ("icmpv6", 2): "packet too big", ("icmpv6", 3): "time exceeded",
    ("icmpv6", 137): "redirect",
}
UNREACH_CODES = {0: "network unreachable", 1: "host unreachable", 2: "protocol unreachable", 3: "port unreachable",
                 4: "fragmentation needed", 9: "network administratively prohibited",
                 10: "host administratively prohibited", 13: "communication administratively prohibited"}


def icmp_rows(events, t0):
    rows = []
    for e in events:
        name = ICMP_NAMES.get((e["family"], e["type"]), f"type {e['type']}")
        detail = UNREACH_CODES.get(e["code"]) if e["family"] == "icmp" and e["type"] == 3 else None
        rows.append({"at_s": round(e["ts"] - t0, 6), "family": e["family"], "type": e["type"], "code": e["code"],
                     "name": name, "detail": detail, "from": e["src"], "to": e["dst"], "mtu": e["mtu"],
                     "original_dst": e["original_dst"], "original_protocol": e["original_protocol"],
                     "original_dst_port": e["original_dst_port"]})
    return rows


def build(extraction, metadata):
    t0 = extraction["first_ts"]
    dns_per_stream = {}
    for t in extraction["dns"]:
        dns_per_stream[t["client"], t["server"]] = dns_per_stream.get((t["client"], t["server"]), 0) + 1

    tcp_flows = [tcp_flow(s, t0) for s in extraction["tcp"].values()]
    udp_flows = [udp_flow(u, t0, dns_per_stream.get((u["client"], u["server"]), 0)) for u in extraction["udp"].values()]

    endpoints = sorted(extraction["endpoints"].items(), key=lambda kv: -(kv[1][1] + kv[1][3]))
    def ep_rows(v6):
        return [{"ip": ip, "tx_packets": e[0], "tx_bytes": e[1], "rx_packets": e[2], "rx_bytes": e[3]}
                for ip, e in endpoints if (":" in ip) == v6][:MAX_ENDPOINTS]

    tls_sessions = [f for f in tcp_flows if f["tls"] and f["tls"]["client_hello_at_s"] is not None]
    summary = {
        "packets": extraction["packets"],
        "bytes": extraction["bytes"],
        "duration_s": round(extraction["last_ts"] - t0, 6),
        "flows": len(tcp_flows) + len(udp_flows),
        "tcp_streams": len(tcp_flows),
        "udp_flows": len(udp_flows),
        "dns_queries": len(extraction["dns"]),
        "tls_sessions": len(tls_sessions),
        "icmp_events": sum(extraction["icmp"]["counts"].values()),
        "ipv4_endpoints": sum(1 for ip in extraction["endpoints"] if ":" not in ip),
        "ipv6_endpoints": sum(1 for ip in extraction["endpoints"] if ":" in ip),
    }
    return {
        "t0": t0,
        "metadata": metadata,
        "summary": summary,
        "endpoints": {"ipv4": ep_rows(False), "ipv6": ep_rows(True)},
        "tcp_flows": tcp_flows,
        "udp_flows": udp_flows,
        "dns": dns_rows(extraction["dns"], t0),
        "dns_all": extraction["dns"],
        "icmp": icmp_rows(extraction["icmp"]["events"], t0),
        "icmp_counts": extraction["icmp"]["counts"],
        "limits": extraction["limits"],
    }


def capture_metadata(info, size_bytes):
    """capinfos columns -> stable keys (values as numbers where possible)."""
    def num(key, cast=float):
        try:
            return cast(info.get(key, ""))
        except ValueError:
            return None

    return {
        "file_type": info.get("File type"),
        "encapsulation": info.get("File encapsulation"),
        "time_precision": info.get("File time precision"),
        "packets": num("Number of packets", int),
        "size_bytes": size_bytes,
        "duration_s": num("Capture duration (seconds)"),
        "first_packet": info.get("First packet time") or info.get("Start time"),
        "last_packet": info.get("Last packet time") or info.get("End time"),
        "avg_packet_rate": num("Average packet rate (packets/sec)"),
        "avg_byte_rate": num("Data byte rate (bytes/sec)"),
        "avg_packet_size": num("Average packet size (bytes)"),
    }
