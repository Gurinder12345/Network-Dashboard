"""
PCAP analysis orchestration: validate -> extract -> rules -> correlate -> assess.

Output JSON separates:
  observations     capture metadata and counts (what is in the file)
  metrics          derived numbers (handshake RTT, DNS latency, ...)
  findings         rule hits: observed / evidence / interpretation / confidence
  assessment       likely / possible / no single root cause, with strong vs supporting evidence
  recommendations  only for causes that have findings, linked by finding id
plus detail tables (flows, DNS, TLS, ICMP, correlation). No payload is ever included.
"""

import os
from statistics import median

from pcap import correlate as correlation
from pcap import report, rules
from pcap.extract import extract
from pcap.tshark import TsharkError, capinfos

RESULT_VERSION = 1
PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": "pcap", b"\xa1\xb2\xc3\xd4": "pcap",
    b"\x4d\x3c\xb2\xa1": "pcap", b"\xa1\xb2\x3c\x4d": "pcap",   # nanosecond pcap
    b"\x0a\x0d\x0d\x0a": "pcapng",
}


def detect_format(path):
    with open(path, "rb") as handle:
        return PCAP_MAGIC.get(handle.read(4))


def _metrics(rep):
    flows = rep["tcp_flows"]
    hs = [f["handshake"]["syn_to_synack_ms"] for f in flows if f["handshake"]["syn_to_synack_ms"] is not None]
    dns = rep["dns_all"]
    latencies = [t["latency"] * 1000 for t in dns if t["latency"] is not None]
    rcodes = {}
    for t in dns:
        if t["answered"]:
            rcodes[t["rcode_name"]] = rcodes.get(t["rcode_name"], 0) + 1
    versions = {}
    for f in flows:
        if f["tls"] and f["tls"]["negotiated_version"]:
            versions[f["tls"]["negotiated_version"]] = versions.get(f["tls"]["negotiated_version"], 0) + 1
    states = {}
    for f in flows:
        states[f["handshake"]["state"]] = states.get(f["handshake"]["state"], 0) + 1
    return {
        "tcp": {
            "streams": len(flows),
            "handshake_states": states,
            "handshake_rtt_ms": {"min": min(hs), "median": round(median(hs), 3), "max": max(hs)} if hs else None,
            "retransmissions": sum(f["tcp"]["retransmissions"] for f in flows),
            "duplicate_acks": sum(f["tcp"]["duplicate_acks"] for f in flows),
            "resets": sum(f["tcp"]["reset_count"] for f in flows),
            "zero_window_events": sum(f["tcp"]["zero_window_from_client"] + f["tcp"]["zero_window_from_server"] for f in flows),
        },
        "dns": {
            "queries": len(dns),
            "answered": sum(1 for t in dns if t["answered"]),
            "unanswered": sum(1 for t in dns if not t["answered"]),
            "rcodes": rcodes,
            "latency_ms": {"min": round(min(latencies), 3), "median": round(median(latencies), 3),
                           "max": round(max(latencies), 3)} if latencies else None,
        },
        "tls": {"sessions": rep["summary"]["tls_sessions"], "negotiated_versions": versions},
        "icmp": {f"{k[0]} type {k[1]} code {k[2]}": n for k, n in rep["icmp_counts"].items()},
    }


def _flows_out(rep):
    flows = rep["tcp_flows"] + rep["udp_flows"]
    flows.sort(key=lambda f: (-len(f["issues"]), -f["bytes"]))
    return flows[:report.MAX_FLOWS], len(flows)


def analyze_capture(path, role, collect_segments):
    fmt = detect_format(path)
    if fmt is None:
        raise TsharkError(f"The {role} file is not a pcap or pcapng capture")
    info = capinfos(path)
    metadata = report.capture_metadata(info, os.path.getsize(path))
    metadata["detected_format"] = fmt
    if not metadata["packets"]:
        raise TsharkError(f"The {role} capture contains no packets")
    return metadata


def run(paths, mode, stage=lambda name: None):
    """paths: {"client": path, "server": path?}. Returns the result dict."""
    stage("validating")
    metadata = {role: analyze_capture(path, role, mode == "dual") for role, path in paths.items()}

    stage("extracting")
    extractions, reps = {}, {}
    for role, path in paths.items():
        extractions[role] = extract(path, collect_segments=(mode == "dual"))
        reps[role] = report.build(extractions[role], metadata[role])

    stage("analyzing")
    client = reps["client"]
    findings = rules.run_single(client, capture="client" if mode == "dual" else None)
    corr = None
    hint = {}

    if mode == "dual":
        server_findings = rules.run_single(reps["server"], capture="server")
        seen = {(f["category"], f["title"]) for f in findings}
        findings += [f for f in server_findings if (f["category"], f["title"]) not in seen]

        stage("correlating")
        corr, corr_findings = correlation.correlate(client, reps["server"], extractions["client"], extractions["server"])
        timed = {e["client_flow"] for e in corr["flows"] if e["server_side_response_ms"] is not None}
        # A single-capture "possible delay" is superseded where both sides measured the flow.
        findings = [f for f in findings if not (f["cause"] == "app_delay" and f["title"].startswith("Possible")
                                                 and f["flows"] and set(f["flows"]) <= timed)]
        findings += corr_findings
        delayed = [e for e in corr["flows"] if e["server_side_response_ms"] is not None]
        if delayed:
            worst = max(delayed, key=lambda e: e["server_side_response_ms"])
            flow = next((f for f in client["tcp_flows"] if f["id"] == worst["client_flow"]), None)
            hint = {"server_delay_s": worst["server_side_response_ms"] / 1000,
                    "handshake_ms": flow["handshake"]["syn_to_synack_ms"] if flow else None}

    rules.number(findings)
    assessment = rules.assess(findings, hint)
    flows, flow_total = _flows_out(client)

    captures = {}
    for role, rep in reps.items():
        captures[role] = {"metadata": rep["metadata"], "summary": rep["summary"], "endpoints": rep["endpoints"]}

    result = {
        "version": RESULT_VERSION,
        "mode": mode,
        "observations": {"captures": captures},
        "metrics": {role: _metrics(rep) for role, rep in reps.items()},
        "flows": flows,
        "flow_total": flow_total,
        "server_flows": (_flows_out(reps["server"])[0][:200] if mode == "dual" else None),
        "dns": {"transactions": client["dns"]},
        "tls": {"sessions": [{"flow": f["id"], "key": f["key"], **f["tls"]} for f in client["tcp_flows"] if f["tls"]][:200]},
        "icmp": {"events": client["icmp"][:200]},
        "correlation": corr,
        "findings": findings,
        "assessment": assessment,
        "recommendations": rules.recommendations(findings),
        "limits": {role: rep["limits"] for role, rep in reps.items()},
        "thresholds": {"app_delay_ms": rules.APP_DELAY_MS, "dns_slow_ms": rules.DNS_SLOW_MS,
                       "tls_slow_ms": rules.TLS_SLOW_MS, "handshake_slow_ms": rules.HANDSHAKE_SLOW_MS},
    }
    return result
