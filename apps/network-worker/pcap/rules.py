"""
Deterministic expert rules -> structured findings -> assessment -> recommendations.

Every finding separates what was OBSERVED (packet facts), its EVIDENCE (numbers / stream
ids), and an INTERPRETATION worded with the certainty the evidence allows. Each finding
names a candidate cause and whether it is `strong` or `supporting` evidence for it.

The assessment never scores numerically:
  - exactly one cause with strong evidence     -> "Likely issue"
  - several causes with strong evidence        -> multiple issues, no single root cause
  - only supporting evidence for one cause     -> "Possible issue"
  - otherwise                                  -> "No single root cause can be established"
"""

import os

APP_DELAY_MS = float(os.getenv("PCAP_APP_DELAY_MS", "1000"))
DNS_SLOW_MS = float(os.getenv("PCAP_DNS_SLOW_MS", "500"))
TLS_SLOW_MS = float(os.getenv("PCAP_TLS_SLOW_MS", "1000"))
HANDSHAKE_SLOW_MS = float(os.getenv("PCAP_HANDSHAKE_SLOW_MS", "500"))
UNANSWERED_DNS_AFTER_S = 2.0
MAX_LISTED = 10

CAUSES = {
    "connection_failure": "TCP connection setup failure",
    "tcp_reset": "Connection reset by an endpoint",
    "packet_loss": "Packet loss or reordering on the path",
    "receiver_window": "Receiver could not keep up (TCP zero window)",
    "app_delay": "Server/application response delay",
    "network_delay": "Network path delay",
    "dns_failure": "DNS resolution failure",
    "dns_delay": "Slow DNS resolution",
    "tls_failure": "TLS handshake failure",
    "mtu": "Path MTU problem",
}

CHECKS = {
    "connection_failure": "TCP handshake failures",
    "tcp_reset": "TCP resets",
    "packet_loss": "TCP retransmissions / loss indicators",
    "receiver_window": "TCP zero-window events",
    "app_delay": "Server/application response delay",
    "network_delay": "Network path delay",
    "dns_failure": "DNS errors",
    "dns_delay": "DNS response delay",
    "tls_failure": "TLS handshake failures",
    "mtu": "ICMP fragmentation-needed / MTU signals",
}

RECOMMENDATIONS = {
    "connection_failure": [
        "Check that the service is listening on the server port and that host/network firewalls allow it.",
        "Verify routing and ACLs in both directions between the client and the server.",
    ],
    "tcp_reset": [
        "Check the resetting endpoint's application/service logs at the reset timestamp.",
        "Check for firewalls, load balancers or IPS devices that may inject or forward resets.",
    ],
    "packet_loss": [
        "Inspect interface error/discard counters and utilisation along the path.",
        "Capture at additional points to locate where segments go missing.",
    ],
    "receiver_window": [
        "Inspect resource pressure (CPU, memory, I/O) and read rate on the host advertising a zero window.",
    ],
    "app_delay": [
        "Inspect application/server logs around the request timestamp.",
        "Measure server-side processing time (for example with a capture or timing on the server).",
    ],
    "network_delay": [
        "Check queueing/latency on the path between the capture points (interface utilisation, QoS, WAN links).",
        "Compare with captures taken at intermediate points.",
    ],
    "dns_failure": [
        "Check the queried names and the resolver's configuration and upstream reachability.",
    ],
    "dns_delay": [
        "Inspect DNS resolver response time and its upstream/forwarder latency.",
    ],
    "tls_failure": [
        "Compare client and server TLS versions, cipher suites, SNI and certificate configuration.",
        "Check the server's TLS/error logs at the alert timestamp.",
    ],
    "mtu": [
        "Check MTU/MSS settings on the path (tunnels, VPNs) and ensure ICMP fragmentation-needed is not filtered.",
    ],
}


class Findings:
    def __init__(self, capture=None):
        self.items = []
        self.capture = capture

    def add(self, severity, category, cause, strength, title, observed, evidence, interpretation, confidence, flows=()):
        finding = {
            "id": None,
            "severity": severity,          # critical | warning | info
            "category": category,          # tcp | dns | tls | icmp | correlation
            "cause": cause,
            "strength": strength,          # strong | supporting | None
            "title": title,
            "observed": observed,
            "evidence": evidence,
            "interpretation": interpretation,
            "confidence": confidence,      # high | medium | low
            "flows": list(flows)[:MAX_LISTED],
        }
        if self.capture:
            finding["capture"] = self.capture
        self.items.append(finding)
        return finding


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tag(flows_by_id, flow_ids, issue):
    for fid in flow_ids:
        flow = flows_by_id.get(fid)
        if flow is not None and issue not in flow["issues"]:
            flow["issues"].append(issue)


def tcp_rules(rep, out):
    flows = rep["tcp_flows"]
    by_id = {f["id"]: f for f in flows}

    # --- connection setup ----------------------------------------------------------
    unanswered = [f for f in flows if f["handshake"]["state"] == "no_syn_ack"]
    for server, group in _group(unanswered, lambda f: (f["server"]["ip"], f["server"]["port"])).items():
        syns = sum(f["handshake"]["syn_packets"] for f in group)
        repeated = syns > len(group)
        ids = [f["id"] for f in group]
        out.add("critical", "tcp", "connection_failure", "strong",
                f"TCP connection attempts to {server[0]}:{server[1]} not answered",
                f"{_plural(syns, 'SYN')} in {_plural(len(group), 'connection attempt')}; no SYN/ACK observed.",
                {"server": f"{server[0]}:{server[1]}", "attempts": len(group), "syn_packets": syns, "streams": [f["stream"] for f in group][:MAX_LISTED]},
                "SYNs were sent but no SYN/ACK was seen at this capture point. The server may be unreachable or not "
                "listening, the SYNs may be filtered, or the reply may be taking a different path.",
                "high" if repeated else "medium", ids)
        _tag(by_id, ids, "No SYN/ACK")

    refused = [f for f in flows if f["handshake"]["state"] == "refused"]
    for server, group in _group(refused, lambda f: (f["server"]["ip"], f["server"]["port"])).items():
        ids = [f["id"] for f in group]
        out.add("critical", "tcp", "connection_failure", "strong",
                f"Connection refused by {server[0]}:{server[1]}",
                f"{_plural(len(group), 'SYN')} answered with RST instead of SYN/ACK.",
                {"server": f"{server[0]}:{server[1]}", "attempts": len(group), "streams": [f["stream"] for f in group][:MAX_LISTED]},
                "The server (or a device in front of it) actively rejected the connection; typically nothing is "
                "listening on that port or a firewall is rejecting it.", "high", ids)
        _tag(by_id, ids, "Refused (RST)")

    slow_retry = [f for f in flows if f["handshake"]["state"] == "complete" and f["handshake"]["syn_packets"] > 1]
    if slow_retry:
        ids = [f["id"] for f in slow_retry]
        out.add("warning", "tcp", "packet_loss", "supporting", "SYN retransmitted before the connection was established",
                f"{_plural(len(slow_retry), 'connection')} needed more than one SYN.",
                {"streams": [{"stream": f["stream"], "syn_packets": f["handshake"]["syn_packets"],
                              "syn_to_synack_ms": f["handshake"]["syn_to_synack_ms"]} for f in slow_retry[:MAX_LISTED]]},
                "The first SYN (or its SYN/ACK) was not answered in time; consistent with loss during connection setup.",
                "medium", ids)
        _tag(by_id, ids, "SYN retry")

    slow_hs = [f for f in flows if f["handshake"]["state"] == "complete" and f["handshake"]["syn_packets"] == 1
               and (f["handshake"]["syn_to_synack_ms"] or 0) >= HANDSHAKE_SLOW_MS]
    if slow_hs:
        ids = [f["id"] for f in slow_hs]
        out.add("info", "tcp", "network_delay", "supporting", "High TCP handshake round-trip time",
                f"SYN -> SYN/ACK took {max(f['handshake']['syn_to_synack_ms'] for f in slow_hs):.0f} ms at most.",
                {"threshold_ms": HANDSHAKE_SLOW_MS,
                 "streams": [{"stream": f["stream"], "syn_to_synack_ms": f["handshake"]["syn_to_synack_ms"]} for f in slow_hs[:MAX_LISTED]]},
                "A slow SYN/ACK reflects path latency and/or server SYN processing as seen from this capture point.",
                "medium", ids)

    # --- resets --------------------------------------------------------------------
    established = [f for f in flows if f["handshake"]["state"] in ("complete", "not_observed", "no_final_ack")]
    for who in ("server", "client"):
        resets = []
        for f in established:
            rst = [r for r in f["tcp"]["resets"] if r["from"] == who]
            if not rst:
                continue
            after_fin = any(x["from"] == who for x in f["tcp"]["fins"]) and any(x["from"] != who for x in f["tcp"]["fins"])
            if after_fin:
                continue  # reset after both sides closed: common abortive teardown, not a fault
            if f["tls"] and any(a["level"] == "fatal" and a["at_s"] <= rst[0]["at_s"] for a in f["tls"]["alerts"]):
                continue  # reset that follows a fatal TLS alert is its consequence (reported by the TLS rule)
            resets.append((f, rst[0]))
        if not resets:
            continue
        ids = [f["id"] for f, _ in resets]
        after_request = [f for f, r in resets if f["timing"]["first_request_at_s"] is not None and f["timing"]["first_response_at_s"] is None]
        out.add("warning", "tcp", "tcp_reset", "strong" if who == "server" else "supporting",
                f"TCP reset from {who}",
                f"{_plural(len(resets), 'connection')} reset by the {who}"
                + (f"; {len(after_request)} before any response to the request." if after_request and who == "server" else "."),
                {"from": who, "connections": len(resets),
                 "resets": [{"stream": f["stream"], "at_s": r["at_s"], "flow": f["key"]} for f, r in resets[:MAX_LISTED]]},
                f"The {who} (or a device acting for it) aborted the connection with RST."
                + (" A reset before any response suggests the request was rejected or the service failed."
                   if after_request and who == "server" else ""),
                "high", ids)
        _tag(by_id, ids, f"RST from {who}")

    # --- loss / reordering indicators ---------------------------------------------
    healthy_setup = [f for f in flows if f["handshake"]["state"] not in ("no_syn_ack", "refused")]
    retrans = [f for f in healthy_setup if f["tcp"]["retransmissions"] - max(f["handshake"]["syn_packets"] - 1, 0) > 0]
    if retrans:
        total = sum(f["tcp"]["retransmissions"] for f in retrans)
        dup = sum(f["tcp"]["duplicate_acks"] for f in retrans)
        fast = sum(f["tcp"]["fast_retransmissions"] for f in retrans)
        lost = sum(f["tcp"]["lost_segments"] + f["tcp"]["ack_lost_segments"] for f in retrans)
        strong = total >= 3 or (fast > 0 and dup >= 2) or lost > 0
        ids = [f["id"] for f in sorted(retrans, key=lambda f: -f["tcp"]["retransmissions"])]
        out.add("warning" if strong else "info", "tcp", "packet_loss", "strong" if strong else "supporting",
                f"{_plural(total, 'TCP retransmission')} detected",
                f"{_plural(total, 'retransmission')} ({fast} fast) and {_plural(dup, 'duplicate ACK')} across "
                f"{_plural(len(retrans), 'connection')}" + (f"; {lost} 'previous segment not captured' / lost-segment markers." if lost else "."),
                {"retransmissions": total, "fast_retransmissions": fast, "duplicate_acks": dup, "lost_segment_markers": lost,
                 "streams": [{"stream": f["stream"], "retransmissions": f["tcp"]["retransmissions"],
                              "duplicate_acks": f["tcp"]["duplicate_acks"], "by": f["tcp"]["retransmissions_by"]}
                             for f in sorted(retrans, key=lambda f: -f["tcp"]["retransmissions"])[:MAX_LISTED]]},
                "Evidence is consistent with packet loss or reordering between the endpoints. A single capture "
                "point cannot tell on which side of it the loss happened.",
                "medium" if strong else "low", ids)
        _tag(by_id, ids, "Retransmissions")

    dup_only = [f for f in healthy_setup if f["tcp"]["duplicate_acks"] and not f["tcp"]["retransmissions"]]
    ooo = [f for f in healthy_setup if f["tcp"]["out_of_order"]]
    if dup_only or ooo:
        ids = [f["id"] for f in dup_only + ooo]
        out.add("info", "tcp", "packet_loss", "supporting", "Duplicate ACKs / out-of-order segments",
                f"{sum(f['tcp']['duplicate_acks'] for f in dup_only)} duplicate ACKs without retransmission, "
                f"{sum(f['tcp']['out_of_order'] for f in ooo)} out-of-order segments.",
                {"streams": sorted({f["stream"] for f in dup_only + ooo})[:MAX_LISTED]},
                "Consistent with reordering on the path; on its own this is weak evidence of loss.", "low", ids)

    spurious = [f for f in healthy_setup if f["tcp"]["spurious_retransmissions"]]
    if spurious:
        ids = [f["id"] for f in spurious]
        out.add("info", "tcp", "network_delay", "supporting", "Spurious retransmissions",
                f"{sum(f['tcp']['spurious_retransmissions'] for f in spurious)} retransmissions of data already acknowledged.",
                {"streams": [f["stream"] for f in spurious][:MAX_LISTED]},
                "The sender retransmitted data the receiver already had, consistent with delayed ACKs or a "
                "retransmission timer firing early (variable path delay), not with loss.", "low", ids)

    # --- receive window ------------------------------------------------------------
    for who in ("client", "server"):
        zw = [f for f in flows if f["tcp"][f"zero_window_from_{who}"]]
        if not zw:
            continue
        ids = [f["id"] for f in zw]
        events = sum(f["tcp"][f"zero_window_from_{who}"] for f in zw)
        out.add("warning", "tcp", "receiver_window", "strong", f"Receiver advertised zero window ({who})",
                f"The {who} advertised a zero receive window {_plural(events, 'time')} in {_plural(len(zw), 'connection')}"
                f" ({sum(f['tcp']['zero_window_probes'] for f in zw)} zero-window probes).",
                {"receiver": who, "events": events,
                 "streams": [{"stream": f["stream"], "zero_window": f["tcp"][f"zero_window_from_{who}"],
                              "probes": f["tcp"]["zero_window_probes"]} for f in zw[:MAX_LISTED]]},
                f"The {who}'s receive buffer was full, so the sender had to pause. This points at the receiving "
                "host/application not reading data fast enough rather than at the network.", "high", ids)
        _tag(by_id, ids, f"Zero window ({who})")

    full = [f for f in flows if f["tcp"]["window_full"]]
    if full:
        out.add("info", "tcp", "receiver_window", "supporting", "Sender filled the receive window",
                f"{sum(f['tcp']['window_full'] for f in full)} window-full events.",
                {"streams": [f["stream"] for f in full][:MAX_LISTED]},
                "The sender was limited by the receiver's advertised window.", "medium", [f["id"] for f in full])

    # --- response timing (single capture point) ------------------------------------
    slow = []
    for f in flows:
        delay = f["timing"]["request_to_first_response_ms"]
        if delay is None or delay < APP_DELAY_MS or f["timing"]["server_spoke_first"]:
            continue
        if f["handshake"]["state"] in ("no_syn_ack", "refused"):
            continue
        loss_before = f["timing"]["retransmissions_before_response"] or 0
        if loss_before > max(f["handshake"]["syn_packets"] - 1, 0) or f["tcp"]["zero_window_from_client"] or f["tcp"]["zero_window_from_server"]:
            continue
        slow.append(f)
    if slow:
        ids = [f["id"] for f in slow]
        worst = max(slow, key=lambda f: f["timing"]["request_to_first_response_ms"])
        hs = worst["handshake"]["syn_to_synack_ms"]
        out.add("warning", "tcp", "app_delay", "supporting", "Possible server/application response delay",
                f"First server data arrived {worst['timing']['request_to_first_response_ms'] / 1000:.2f} s after the request"
                + (f"; handshake RTT {hs:.0f} ms." if hs is not None else ".")
                + " No retransmissions or zero-window events before the response.",
                {"threshold_ms": APP_DELAY_MS,
                 "streams": [{"stream": f["stream"], "flow": f["key"], "request_to_first_response_ms": f["timing"]["request_to_first_response_ms"],
                              "handshake_rtt_ms": f["handshake"]["syn_to_synack_ms"]} for f in slow[:MAX_LISTED]]},
                "With transport indicators clean, the pause is consistent with server/application processing time. "
                "From a single capture point it cannot be fully separated from path delay; a server-side capture would confirm it.",
                "medium" if hs is not None else "low", ids)
        _tag(by_id, ids, "Slow response")


def dns_rules(rep, out):
    rows = rep["dns_all"]
    by_rcode = {}
    for t in rows:
        if t["answered"] and t["rcode_name"] not in (None, "NOERROR"):
            by_rcode.setdefault(t["rcode_name"], []).append(t)

    def names(ts):
        return sorted({t["name"] for t in ts if t["name"]})[:MAX_LISTED]

    if "SERVFAIL" in by_rcode or "REFUSED" in by_rcode:
        bad = by_rcode.get("SERVFAIL", []) + by_rcode.get("REFUSED", [])
        out.add("warning", "dns", "dns_failure", "strong", "DNS server failure responses",
                f"{_plural(len(bad), 'query')} answered with SERVFAIL/REFUSED.",
                {"count": len(bad), "names": names(bad), "rcodes": sorted({t["rcode_name"] for t in bad})},
                "The resolver could not (or would not) resolve these names. If the affected application uses them, "
                "name resolution is failing for it.", "high")
    if "NXDOMAIN" in by_rcode:
        nx = by_rcode["NXDOMAIN"]
        out.add("info", "dns", "dns_failure", "supporting", "NXDOMAIN returned",
                f"{_plural(len(nx), 'query')} returned NXDOMAIN (name does not exist).",
                {"count": len(nx), "names": names(nx)},
                "These names do not exist according to the resolver. Search-suffix lookups often produce harmless "
                "NXDOMAINs; it matters only if the application needed one of these names.", "high")

    slow = [t for t in rows if t["answered"] and t["latency"] is not None and t["latency"] * 1000 >= DNS_SLOW_MS]
    if slow:
        worst = max(slow, key=lambda t: t["latency"])
        out.add("warning", "dns", "dns_delay", "strong", f"DNS response took {worst['latency']:.2f} s",
                f"{_plural(len(slow), 'query')} took at least {DNS_SLOW_MS:.0f} ms to be answered (slowest {worst['latency'] * 1000:.0f} ms, {worst['name']}).",
                {"threshold_ms": DNS_SLOW_MS, "queries": [{"name": t["name"], "latency_ms": round(t["latency"] * 1000, 1),
                                                          "server": f"{t['server'][0]}:{t['server'][1]}"} for t in slow[:MAX_LISTED]]},
                "Name resolution added this delay before the client could connect.", "high")

    repeated = [t for t in rows if t["repeats"]]
    if repeated:
        out.add("warning", "dns", "dns_delay", "supporting", "DNS query repeated before response",
                f"{_plural(len(repeated), 'query')} sent more than once before an answer arrived.",
                {"queries": [{"name": t["name"], "repeats": t["repeats"], "answered": t["answered"]} for t in repeated[:MAX_LISTED]]},
                "The client retried because the first query was not answered in time (lost or slow resolver).", "medium")

    unanswered = [t for t in rows if not t["answered"] and t["capture_after_query_s"] >= UNANSWERED_DNS_AFTER_S]
    if unanswered:
        out.add("warning", "dns", "dns_failure", "strong", "DNS query without response",
                f"{_plural(len(unanswered), 'query')} received no response although the capture continued for at least {UNANSWERED_DNS_AFTER_S:.0f} s.",
                {"queries": [{"name": t["name"], "server": f"{t['server'][0]}:{t['server'][1]}",
                              "capture_after_query_s": t["capture_after_query_s"]} for t in unanswered[:MAX_LISTED]]},
                "The resolver did not answer within the observed window (query or response lost, or resolver unreachable).",
                "medium")


def tls_rules(rep, out):
    flows = rep["tcp_flows"]
    by_id = {f["id"]: f for f in flows}
    sessions = [f for f in flows if f["tls"]]

    fatal = [(f, a) for f in sessions for a in f["tls"]["alerts"] if a["level"] == "fatal" and a["description"] != "close_notify"]
    for desc, group in _group(fatal, lambda fa: (fa[1]["description"], fa[1]["from"])).items():
        ids = [f["id"] for f, _ in group]
        out.add("critical", "tls", "tls_failure", "strong", f"TLS alert: {desc[0]} (from {desc[1]})",
                f"{_plural(len(group), 'fatal TLS alert')} '{desc[0]}' sent by the {desc[1]}.",
                {"alert": desc[0], "from": desc[1],
                 "sessions": [{"stream": f["stream"], "sni": f["tls"]["sni"], "at_s": a["at_s"],
                               "offered_versions": f["tls"]["offered_versions"]} for f, a in group[:MAX_LISTED]]},
                "The TLS handshake/session was terminated with a fatal alert, so the application could not exchange data.",
                "high", ids)
        _tag(by_id, ids, f"TLS alert {desc[0]}")

    warn = [(f, a) for f in sessions for a in f["tls"]["alerts"] if a["level"] == "warning" and a["description"] != "close_notify"]
    if warn:
        out.add("warning", "tls", "tls_failure", "supporting", "TLS warning alerts",
                f"{_plural(len(warn), 'warning-level TLS alert')}: {', '.join(sorted({a['description'] for _, a in warn}))}.",
                {"sessions": [{"stream": f["stream"], "alert": a["description"], "from": a["from"]} for f, a in warn[:MAX_LISTED]]},
                "Warning alerts do not necessarily end the session but indicate a TLS-level complaint.", "medium",
                [f["id"] for f, _ in warn])

    stalled = []
    for f in sessions:
        t = f["tls"]
        if t["client_hello_at_s"] is None or t["server_hello_at_s"] is not None or any(a["level"] == "fatal" for a in t["alerts"]):
            continue
        closed = f["tcp"]["reset_count"] > 0 or f["tcp"]["fins"]
        waited = rep["summary"]["duration_s"] - t["client_hello_at_s"]
        if closed or waited >= 2:
            stalled.append(f)
    if stalled:
        ids = [f["id"] for f in stalled]
        out.add("warning", "tls", "tls_failure", "strong", "TLS handshake did not progress past ClientHello",
                f"{_plural(len(stalled), 'session')} sent a ClientHello but no ServerHello was seen.",
                {"sessions": [{"stream": f["stream"], "sni": f["tls"]["sni"], "offered_versions": f["tls"]["offered_versions"]}
                              for f in stalled[:MAX_LISTED]]},
                "The server (or a middlebox) did not answer the TLS handshake; the connection was closed or stalled.",
                "medium", ids)
        _tag(by_id, ids, "TLS stalled")

    slow = [f for f in sessions if (f["tls"]["client_hello_to_server_hello_ms"] or 0) >= TLS_SLOW_MS]
    if slow:
        out.add("warning", "tls", "app_delay", "supporting", "Slow TLS ServerHello",
                f"ServerHello arrived up to {max(f['tls']['client_hello_to_server_hello_ms'] for f in slow) / 1000:.2f} s after ClientHello.",
                {"sessions": [{"stream": f["stream"], "sni": f["tls"]["sni"],
                               "client_hello_to_server_hello_ms": f["tls"]["client_hello_to_server_hello_ms"]} for f in slow[:MAX_LISTED]]},
                "The server was slow to answer the TLS handshake (server load or path delay).", "low", [f["id"] for f in slow])

    old = [f for f in sessions if f["tls"]["negotiated_version"] in ("SSL 3.0", "TLS 1.0", "TLS 1.1")]
    if old:
        out.add("info", "tls", None, None, "Legacy TLS version negotiated",
                f"{_plural(len(old), 'session')} negotiated {', '.join(sorted({f['tls']['negotiated_version'] for f in old}))}.",
                {"sessions": [{"stream": f["stream"], "sni": f["tls"]["sni"], "version": f["tls"]["negotiated_version"]} for f in old[:MAX_LISTED]]},
                "Not a fault by itself, but these versions are deprecated.", "high", [f["id"] for f in old])


def icmp_rules(rep, out):
    events = rep["icmp"]
    too_big = [e for e in events if (e["family"] == "icmp" and e["type"] == 3 and e["code"] == 4)
               or (e["family"] == "icmpv6" and e["type"] == 2)]
    if too_big:
        mtus = sorted({e["mtu"] for e in too_big if e["mtu"]})
        out.add("warning", "icmp", "mtu", "strong",
                f"ICMP fragmentation needed{f' (next-hop MTU {mtus[0]})' if mtus else ''}",
                f"{_plural(len(too_big), 'fragmentation-needed / packet-too-big message')} from "
                f"{', '.join(sorted({e['from'] for e in too_big}))}.",
                {"messages": len(too_big), "mtu": mtus,
                 "events": [{"at_s": e["at_s"], "from": e["from"], "to": e["to"], "mtu": e["mtu"], "original_dst": e["original_dst"],
                             "original_dst_port": e["original_dst_port"]} for e in too_big[:MAX_LISTED]]},
                "A device on the path reported that a packet was larger than its next-hop MTU. Path MTU discovery "
                "depends on these messages; where they are filtered, large packets can be silently dropped.", "high")

    unreachable = [e for e in events if e["name"] == "destination unreachable" and not (e["family"] == "icmp" and e["code"] == 4)]
    for detail, group in _group(unreachable, lambda e: e["detail"] or f"code {e['code']}").items():
        out.add("warning", "icmp", "connection_failure", "supporting", f"ICMP destination unreachable ({detail})",
                f"{_plural(len(group), 'message')} from {', '.join(sorted({e['from'] for e in group}))}.",
                {"events": [{"at_s": e["at_s"], "from": e["from"], "to": e["to"], "original_dst": e["original_dst"],
                             "original_protocol": e["original_protocol"], "original_dst_port": e["original_dst_port"]} for e in group[:MAX_LISTED]]},
                "A router or host reported that the destination could not be reached.", "high")

    exceeded = [e for e in events if e["name"] == "time exceeded"]
    if exceeded:
        out.add("info", "icmp", None, None, "ICMP time exceeded",
                f"{_plural(len(exceeded), 'TTL-exceeded message')} from {', '.join(sorted({e['from'] for e in exceeded})[:5])}.",
                {"events": [{"at_s": e["at_s"], "from": e["from"], "original_dst": e["original_dst"]} for e in exceeded[:MAX_LISTED]]},
                "Normal for traceroute; otherwise can indicate a routing loop.", "medium")

    redirects = [e for e in events if e["name"] == "redirect"]
    if redirects:
        out.add("info", "icmp", None, None, "ICMP redirect",
                f"{_plural(len(redirects), 'redirect')} from {', '.join(sorted({e['from'] for e in redirects}))}.",
                {"events": [{"at_s": e["at_s"], "from": e["from"], "to": e["to"]} for e in redirects[:MAX_LISTED]]},
                "A router told a host to use a different next hop.", "high")


def _group(items, key):
    groups = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return groups


def run_single(rep, capture=None):
    out = Findings(capture)
    tcp_rules(rep, out)
    dns_rules(rep, out)
    tls_rules(rep, out)
    icmp_rules(rep, out)
    return out.items


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def number(findings):
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["category"]))
    for i, finding in enumerate(findings, start=1):
        finding["id"] = f"F{i}"
    return findings


def assess(findings, summary_hint=None):
    causes = {}
    for f in findings:
        if f["cause"] and f["strength"]:
            entry = causes.setdefault(f["cause"], {"strong": [], "supporting": []})
            entry[f["strength"]].append(f)

    strong = [c for c, e in causes.items() if e["strong"]]
    checked_clear = [CHECKS[c] for c in CHECKS if c not in causes]

    def evidence(items):
        return [{"finding": f["id"], "text": f"{f['title']}: {f['observed']}"} for f in items]

    if len(strong) == 1:
        cause = strong[0]
        result = {"label": "likely", "cause": cause, "issue": CAUSES[cause],
                  "strong_evidence": evidence(causes[cause]["strong"]),
                  "supporting_evidence": evidence(causes[cause]["supporting"])}
    elif len(strong) > 1:
        result = {"label": "multiple", "cause": None, "issue": None,
                  "candidates": [{"cause": c, "issue": CAUSES[c], "strong_evidence": evidence(causes[c]["strong"])} for c in strong],
                  "strong_evidence": [], "supporting_evidence": []}
    elif len(causes) == 1 and any(f["severity"] != "info" for f in next(iter(causes.values()))["supporting"]):
        # Info-only hints (e.g. one NXDOMAIN) never become a headline.
        cause = next(iter(causes))
        result = {"label": "possible", "cause": cause, "issue": "Possible " + CAUSES[cause][0].lower() + CAUSES[cause][1:],
                  "strong_evidence": [], "supporting_evidence": evidence(causes[cause]["supporting"])}
    else:
        result = {"label": "none", "cause": None, "issue": None, "strong_evidence": [],
                  "supporting_evidence": [e for c in causes.values() for e in evidence(c["supporting"])]}

    result["no_evidence_for"] = checked_clear
    result["summary"] = summary_text(result, findings, summary_hint or {})
    return result


def summary_text(result, findings, hint):
    if result["label"] == "multiple":
        names = "; ".join(c["issue"] for c in result["candidates"])
        return f"Several independent problems were observed ({names}). No single root cause can be established from this capture."
    if result["label"] == "none":
        if not [f for f in findings if f["severity"] != "info"]:
            return "No transport, DNS, TLS or ICMP problems were detected by the rule set in this capture."
        return "No single root cause can be established from this capture."

    cause = result["cause"]
    if cause == "app_delay" and hint.get("server_delay_s") is not None:
        hs = hint.get("handshake_ms")
        return ("Transport is healthy. "
                + (f"The TCP handshake completed in {hs:.0f} ms " if hs is not None else "The TCP handshake completed ")
                + "with no retransmissions before the request. The server capture shows the request arriving and the "
                f"first response data leaving {hint['server_delay_s']:.1f} s later. Evidence points toward "
                "server/application processing delay rather than transport loss.")
    if cause == "app_delay":
        return ("The connection was set up normally and no loss indicators preceded the response, but the first server "
                "data arrived well after the request. From this single capture point this is consistent with "
                "server/application delay; a server-side capture would confirm it.")

    lead = result["strong_evidence"] or result["supporting_evidence"]
    observed = lead[0]["text"] if lead else ""
    prefix = "Likely issue" if result["label"] == "likely" else "Possible issue"
    return f"{prefix}: {CAUSES[cause]}. {observed}"


def recommendations(findings):
    recs, seen = [], {}
    for f in findings:
        if not f["cause"] or not f["strength"]:
            continue
        for text in RECOMMENDATIONS.get(f["cause"], []):
            if text in seen:
                if f["id"] not in seen[text]["findings"]:
                    seen[text]["findings"].append(f["id"])
                continue
            entry = {"cause": f["cause"], "text": text, "findings": [f["id"]]}
            seen[text] = entry
            recs.append(entry)
    return recs
