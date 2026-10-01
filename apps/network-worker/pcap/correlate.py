"""
Client + server capture correlation.

Flows are matched by 5-tuple (each capture's own client/server inference), falling back
to (server port, client initial sequence number) so a NATed connection still matches.
Segments are matched by TCP identity, not by time: (direction, raw seq, raw ack, payload
length, SYN/ACK/RST/FIN). Duplicates (retransmissions) are compared as counts.

Clock offset: the capture clocks may differ. For segments seen exactly once on each side,
    client->server: t_server - t_client = offset + one_way_forward
    server->client: t_client - t_server = -offset + one_way_reverse
Using the minimum of each (least queueing), offset = (min_fwd - min_rev) / 2, assuming a
symmetric path. Confidence is high only with enough samples in both directions and good
agreement between the minimum- and median-based estimates; otherwise no one-way latency
claims are made (presence comparison by sequence numbers still works).

Response timing needs no clock offset: the server-side time between the request arriving
and the response leaving is measured on the server's own clock, and the client-side
request->response time on the client's.

All conclusions are "between the capture points"; two captures cannot localise a problem
to a specific device between them.
"""

from collections import Counter
from statistics import median

from pcap.rules import APP_DELAY_MS, Findings

MIN_SAMPLES_HIGH = 5
OFFSET_AGREEMENT_S = 0.005
MAX_LISTED = 10


def _tuple(flow):
    return (flow["client"]["ip"], flow["client"]["port"], flow["server"]["ip"], flow["server"]["port"])


def _isn(segments_by_stream, stream, client_ep):
    for seg in segments_by_stream.get(stream, ()):
        if seg[1] == client_ep and seg[5] & 0x02 and not seg[5] & 0x10:
            return seg[2]
    return None


def _by_stream(segments):
    out = {}
    for seg in segments or ():
        out.setdefault(seg[0], []).append(seg)
    return out


def match_flows(client_rep, server_rep, client_segs, server_segs):
    server_by_tuple = {_tuple(f): f for f in server_rep["tcp_flows"]}
    server_by_isn = {}
    for f in server_rep["tcp_flows"]:
        isn = _isn(server_segs, f["stream"], (f["client"]["ip"], f["client"]["port"]))
        if isn is not None:
            server_by_isn[(f["server"]["port"], isn)] = f
    pairs, used = [], set()
    for f in client_rep["tcp_flows"]:
        other = server_by_tuple.get(_tuple(f))
        how = "5-tuple"
        if other is None:
            isn = _isn(client_segs, f["stream"], (f["client"]["ip"], f["client"]["port"]))
            other = server_by_isn.get((f["server"]["port"], isn)) if isn is not None else None
            how = "sequence (NAT)"
        if other is not None and other["id"] not in used:
            used.add(other["id"])
            pairs.append((f, other, how))
    return pairs


def _keyed(segments, client_ep):
    """direction-tagged identity -> list of timestamps."""
    out = {}
    for _, ep, seq, ack, length, flags, ts in segments:
        direction = "c2s" if ep == client_ep else "s2c"
        out.setdefault((direction, seq, ack, length, flags), []).append(ts)
    return out


def estimate_offset(pairs_data):
    fwd, rev = [], []
    for c_keys, s_keys in pairs_data:
        for key, c_times in c_keys.items():
            s_times = s_keys.get(key)
            if not s_times or len(c_times) != 1 or len(s_times) != 1:
                continue
            if key[0] == "c2s":
                fwd.append(s_times[0] - c_times[0])
            else:
                rev.append(c_times[0] - s_times[0])

    if not fwd or not rev:
        return {"estimated_clock_offset_ms": None, "confidence": "none" if not (fwd or rev) else "low",
                "forward_samples": len(fwd), "reverse_samples": len(rev),
                "note": "Matched packets in both directions are needed to separate clock offset from path delay."}

    by_min = (min(fwd) - min(rev)) / 2
    by_median = (median(fwd) - median(rev)) / 2
    path_rtt = min(fwd) + min(rev)
    agree = abs(by_min - by_median) <= OFFSET_AGREEMENT_S
    if len(fwd) >= MIN_SAMPLES_HIGH and len(rev) >= MIN_SAMPLES_HIGH and agree:
        confidence = "high"
    elif agree or (len(fwd) >= 2 and len(rev) >= 2):
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "estimated_clock_offset_ms": round(by_min * 1000, 3),
        "median_based_offset_ms": round(by_median * 1000, 3),
        "path_rtt_between_capture_points_ms": round(path_rtt * 1000, 3),
        "confidence": confidence,
        "forward_samples": len(fwd), "reverse_samples": len(rev),
        "assumption": "symmetric one-way delay between the capture points",
    }


def correlate(client_rep, server_rep, client_ext, server_ext):
    client_segs, server_segs = _by_stream(client_ext["segments"]), _by_stream(server_ext["segments"])
    pairs = match_flows(client_rep, server_rep, client_segs, server_segs)

    keyed = []
    for cf, sf, _ in pairs:
        c_keys = _keyed(client_segs.get(cf["stream"], ()), (cf["client"]["ip"], cf["client"]["port"]))
        s_keys = _keyed(server_segs.get(sf["stream"], ()), (sf["client"]["ip"], sf["client"]["port"]))
        keyed.append((c_keys, s_keys))
    offset = estimate_offset(keyed)

    flows = []
    totals = Counter()
    for (cf, sf, how), (c_keys, s_keys) in zip(pairs, keyed):
        # Only compare inside the window both captures cover for this flow (own clocks).
        matched = [k for k in c_keys if k in s_keys]
        if matched:
            c_lo = min(c_keys[k][0] for k in matched)
            c_hi = max(c_keys[k][-1] for k in matched)
            s_lo = min(s_keys[k][0] for k in matched)
            s_hi = max(s_keys[k][-1] for k in matched)
        else:
            c_lo = c_hi = s_lo = s_hi = None

        def missing(src_keys, dst_keys, direction, lo, hi):
            out = []
            for key, times in src_keys.items():
                if key[0] != direction:
                    continue
                gap = len(times) - len(dst_keys.get(key, ()))
                in_window = [t for t in times if lo is not None and lo <= t <= hi]
                if gap > 0 and in_window:
                    out.append({"seq": key[1], "ack": key[2], "payload_bytes": key[3], "flags": key[4],
                                "seen": len(times), "seen_other_side": len(dst_keys.get(key, ())), "missing": gap})
            return out

        lost_fwd = missing(c_keys, s_keys, "c2s", c_lo, c_hi)      # client outbound, not at server
        lost_rev = missing(s_keys, c_keys, "s2c", s_lo, s_hi)      # server outbound, not at client
        unseen_by_client = missing(s_keys, c_keys, "c2s", s_lo, s_hi)  # server saw a client packet the client capture lacks
        delivered_fwd = sum(min(len(t), len(s_keys.get(k, ()))) for k, t in c_keys.items() if k[0] == "c2s")
        delivered_rev = sum(min(len(t), len(c_keys.get(k, ()))) for k, t in s_keys.items() if k[0] == "s2c")

        server_rt = sf["timing"]["request_to_first_response_ms"]
        client_rt = cf["timing"]["request_to_first_response_ms"]
        entry = {
            "client_flow": cf["id"], "server_flow": sf["id"], "matched_by": how, "key": cf["key"],
            "delivered_client_to_server": delivered_fwd, "delivered_server_to_client": delivered_rev,
            "missing_at_server": sum(x["missing"] for x in lost_fwd),
            "missing_at_client": sum(x["missing"] for x in lost_rev),
            "client_capture_missing": sum(x["missing"] for x in unseen_by_client),
            "missing_at_server_examples": lost_fwd[:MAX_LISTED],
            "missing_at_client_examples": lost_rev[:MAX_LISTED],
            "server_side_response_ms": server_rt,
            "client_side_response_ms": client_rt,
            "network_share_of_response_ms": round(client_rt - server_rt, 3) if client_rt is not None and server_rt is not None else None,
        }
        cf["correlation"] = entry
        flows.append(entry)
        totals["missing_at_server"] += entry["missing_at_server"]
        totals["missing_at_client"] += entry["missing_at_client"]
        totals["delivered_fwd"] += delivered_fwd
        totals["delivered_rev"] += delivered_rev

    matched_client_ids = {cf["id"] for cf, _, _ in pairs}
    matched_server_ids = {sf["id"] for _, sf, _ in pairs}
    only_client = [f["key"] for f in client_rep["tcp_flows"] if f["id"] not in matched_client_ids]
    only_server = [f["key"] for f in server_rep["tcp_flows"] if f["id"] not in matched_server_ids]

    findings = Findings("correlation")
    by_id = {f["id"]: f for f in client_rep["tcp_flows"]}

    lost = [e for e in flows if e["missing_at_server"]]
    if lost:
        n = sum(e["missing_at_server"] for e in lost)
        findings.add("warning", "correlation", "packet_loss", "strong",
                     "Client packets did not reach the server capture point",
                     f"{n} client->server segment{'s were' if n != 1 else ' was'} seen leaving at the client capture but not at the server capture.",
                     {"flows": [{"flow": e["key"], "missing": e["missing_at_server"], "examples": e["missing_at_server_examples"][:3]} for e in lost[:MAX_LISTED]]},
                     "Loss is likely between the capture points in the client-to-server direction.", "high",
                     [e["client_flow"] for e in lost])
        for e in lost:
            by_id[e["client_flow"]]["issues"].append("Lost client->server")

    lost_r = [e for e in flows if e["missing_at_client"]]
    if lost_r:
        n = sum(e["missing_at_client"] for e in lost_r)
        findings.add("warning", "correlation", "packet_loss", "strong",
                     "Server packets did not reach the client capture point",
                     f"{n} server->client segment{'s were' if n != 1 else ' was'} seen leaving at the server capture but not at the client capture.",
                     {"flows": [{"flow": e["key"], "missing": e["missing_at_client"], "examples": e["missing_at_client_examples"][:3]} for e in lost_r[:MAX_LISTED]]},
                     "Loss is likely between the capture points in the reverse (server-to-client) path.", "high",
                     [e["client_flow"] for e in lost_r])
        for e in lost_r:
            by_id[e["client_flow"]]["issues"].append("Lost server->client")

    server_delay = [e for e in flows if e["server_side_response_ms"] is not None and e["server_side_response_ms"] >= APP_DELAY_MS
                    and (e["client_side_response_ms"] is None or e["server_side_response_ms"] >= 0.8 * e["client_side_response_ms"])]
    if server_delay:
        worst = max(server_delay, key=lambda e: e["server_side_response_ms"])
        findings.add("warning", "correlation", "app_delay", "strong", "Delay at the server/application side",
                     f"The request reached the server capture and the first response left the server "
                     f"{worst['server_side_response_ms'] / 1000:.2f} s later (client saw {worst['client_side_response_ms'] / 1000:.2f} s in total)."
                     if worst["client_side_response_ms"] is not None else
                     f"The first response left the server {worst['server_side_response_ms'] / 1000:.2f} s after the request arrived.",
                     {"threshold_ms": APP_DELAY_MS,
                      "flows": [{"flow": e["key"], "server_side_response_ms": e["server_side_response_ms"],
                                 "client_side_response_ms": e["client_side_response_ms"]} for e in server_delay[:MAX_LISTED]]},
                     "Most of the wait happened between the request arriving at the server capture point and the response "
                     "leaving it, i.e. at the server/application side of that capture point.", "high",
                     [e["client_flow"] for e in server_delay])
        for e in server_delay:
            if "Server delay" not in by_id[e["client_flow"]]["issues"]:
                by_id[e["client_flow"]]["issues"].append("Server delay")

    rtt = offset.get("path_rtt_between_capture_points_ms") or 0
    net_delay = [e for e in flows if e["network_share_of_response_ms"] is not None
                 and e["network_share_of_response_ms"] >= max(APP_DELAY_MS / 2, 3 * rtt) and e not in server_delay]
    if net_delay:
        worst = max(net_delay, key=lambda e: e["network_share_of_response_ms"])
        findings.add("warning", "correlation", "network_delay", "strong", "Response delayed between the capture points",
                     f"The server answered in {worst['server_side_response_ms']:.0f} ms at its capture point, but the client "
                     f"saw the response {worst['client_side_response_ms']:.0f} ms after the request.",
                     {"path_rtt_between_capture_points_ms": offset.get("path_rtt_between_capture_points_ms"),
                      "flows": [{"flow": e["key"], "server_side_response_ms": e["server_side_response_ms"],
                                 "client_side_response_ms": e["client_side_response_ms"],
                                 "network_share_ms": e["network_share_of_response_ms"]} for e in net_delay[:MAX_LISTED]]},
                     "The extra time was spent between the capture points (network path or devices on it), not at the server.",
                     "medium", [e["client_flow"] for e in net_delay])

    if flows and not lost and not lost_r:
        findings.add("info", "correlation", None, None, "Network path delivered all matched packets",
                     f"{totals['delivered_fwd']} client->server and {totals['delivered_rev']} server->client segments were seen at both capture points.",
                     {"matched_flows": len(flows)},
                     "No loss between the capture points for the matched flows.", "high", [e["client_flow"] for e in flows])

    if only_client or only_server:
        findings.add("info", "correlation", None, None, "Flows seen at only one capture point",
                     f"{len(only_client)} flow(s) only in the client capture, {len(only_server)} only in the server capture.",
                     {"only_client": only_client[:MAX_LISTED], "only_server": only_server[:MAX_LISTED]},
                     "These may be unrelated traffic, filtered by a capture filter, or outside the other capture's time window.",
                     "medium")

    if any(e["client_capture_missing"] for e in flows):
        findings.add("info", "correlation", None, None, "Client capture is missing some of its own packets",
                     "The server capture contains client->server segments that the client capture does not.",
                     {"flows": [{"flow": e["key"], "missing": e["client_capture_missing"]} for e in flows if e["client_capture_missing"]][:MAX_LISTED]},
                     "Usually packets dropped by the capture tool itself (or TSO/offload on the client); treat client-side counts with care.",
                     "medium")

    return {
        "matched_flows": len(flows),
        "only_client_flows": len(only_client),
        "only_server_flows": len(only_server),
        "clock": offset,
        "flows": flows[:200],
        "totals": dict(totals),
        "segments_truncated": bool(client_ext["limits"]["segments_truncated"] or server_ext["limits"]["segments_truncated"]),
    }, findings.items
