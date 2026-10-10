"""
Local mock API + static server for the UI browser tests (synthetic data only; never
contacts a device or the real API). Serves the production build (dist/) and the API
routes the Changes, Approvals, Devices and Overview pages use. Records POSTed precheck bodies at
GET /__requests so tests can assert exactly what the browser sent.

    python e2e/mock_api.py dist 58095
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DIST = sys.argv[1]
NOW = datetime.now(timezone.utc)


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat()


DEVICES = [
    {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "192.0.2.31", "platform": "dell_os6", "enabled": True},
    {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "192.0.2.10", "platform": "dell_os10", "enabled": True},
]
for d in DEVICES:
    d.update(health_status="healthy", last_check_at=ago(seconds=20), last_success_at=ago(seconds=20), response_time_ms=900,
             tcp_reachable=True, ssh_reachable=True, cli_reachable=True, last_error=None)
    d.update(operation_state="normal", active_change_id=None, change_started_at=None, polling_suppressed=False,
             current_operation=None, health_grace=False)
# Device coordination: an approved change is executing on Kenda-Core-1 (health stays healthy).
DEVICES[1].update(operation_state="change_in_progress", active_change_id="a0000000-0000-4000-8000-000000000184",
                  change_started_at=ago(seconds=40), polling_suppressed=True, current_operation="apply")

MULTI = [{"order": 0, "parent": "interface ethernet1/1/18", "commands": ["description APP-SERVER", "switchport mode trunk"]},
         {"order": 1, "parent": "interface ethernet1/1/19", "commands": ["switchport access vlan 50"]},
         {"order": 2, "parent": None, "commands": ["ip routing"]}]
PREVIEW = ("! Block 1\ninterface ethernet1/1/18\n description APP-SERVER\n switchport mode trunk\n\n"
           "! Block 2\ninterface ethernet1/1/19\n switchport access vlan 50\n\n! Block 3 - Global\nip routing")
CHECKS = {"structural_validation": "PASS", "device_connectivity": "PASS", "current_config_capture": "PASS",
          "semantic_verification": "PARTIAL"}


def approval(id_, status, blocks, preview, legacy=False, execution=None, summary=True):
    return {"id": id_, "device_id": 12, "backup_job_id": "b0000000-0000-4000-8000-000000000001", "requested_by": "system",
            "approved_by": None if status == "pending" else "reviewer", "status": status,
            "config_lines": [l for b in blocks for l in ([b["parent"]] if b["parent"] else []) + b["commands"]],
            "config_parents": None, "created_at": ago(minutes=5), "approved_at": None, "cancelled_by": None,
            "cancelled_at": None, "cancellation_reason": None, "config_blocks": blocks, "legacy_single_block": legacy,
            "block_count": len(blocks), "command_count": sum(len(b["commands"]) for b in blocks), "cli_preview": preview,
            "verification_commands": ["show vlan"] if not legacy else [],
            "precheck_summary": {"overall": "PASS", "checks": CHECKS, "block_count": len(blocks),
                                 "command_count": sum(len(b["commands"]) for b in blocks),
                                 "blocks": [{"index": i, "parent": b["parent"], "status": "PASS", "issues": [],
                                             "commands": [{"status": "not_available", "detail": "x"} for _ in b["commands"]]}
                                            for i, b in enumerate(blocks)]} if summary else None,
            "execution_result": execution}


PARTIAL = {"outcome": "partial_apply", "execution": "partial", "semantic": "verified", "started_at": ago(minutes=3),
           "finished_at": ago(minutes=2), "job_id": "c0000000-0000-4000-8000-000000000001",
           "blocks": [{"index": 0, "parent": "interface ethernet1/1/18", "command_count": 2, "status": "applied", "failed_command": None, "error": None},
                      {"index": 1, "parent": "interface ethernet1/1/19", "command_count": 1, "status": "failed", "failed_command": 1,
                       "failed_step": "command", "error": "% Error: VLAN 50 does not exist."},
                      {"index": 2, "parent": None, "command_count": 1, "status": "not_attempted", "failed_command": None, "error": None}],
           "post_check": {"read": "ok", "semantic": "verified", "error": None,
                          "blocks": [{"index": 0, "found": True, "commands": [
                              {"command": "description APP-SERVER", "status": "verified", "detail": "current description: APP-SERVER"},
                              {"command": "switchport mode trunk", "status": "verified", "detail": "mode is trunk"}]}]},
           "verification": [{"command": "show vlan", "ok": True, "output": "VLAN 1 default"}],
           "error": "PARTIAL APPLY on Kenda-Core-1: block 1 applied, block 2 failed, block 3 not attempted."}

APPROVALS = [
    approval("a0000000-0000-4000-8000-000000000001", "pending", MULTI, PREVIEW),
    approval("a0000000-0000-4000-8000-000000000002", "failed", MULTI, PREVIEW, execution=PARTIAL),
    approval("a0000000-0000-4000-8000-000000000003", "applied",
             [{"order": 0, "parent": "interface Tw1/0/3", "commands": ["description OLD-CHANGE"]}],
             "! Block 1\ninterface Tw1/0/3\n description OLD-CHANGE", legacy=True, summary=False),
]

PRECHECK = {"request_id": "11111111-1111-4111-8111-111111111111", "state": "completed", "error": None, "result": {
    "status": "pending_approval", "target_host": "Kenda-Core-1", "platform": "dell_os10", "ready_for_approval": True,
    "backup_required": True, "rejection_reasons": [], "block_count": 3, "command_count": 4,
    "dry_run": {"would_change": True, "verification_method": "running-configuration", "already_present": [],
                "proposed_changes": [], "command_results": [], "overall": "PASS", "checks": CHECKS,
                "blocks": [{"index": 0, "parent": "interface ethernet1/1/18", "global": False, "command_count": 2, "status": "PASS",
                            "issues": [], "current_config_found": True, "current_config": ["no shutdown", "switchport access vlan 20"],
                            "commands": [{"command": "description APP-SERVER", "status": "not_present", "detail": "current description: none"},
                                         {"command": "switchport mode trunk", "status": "not_present", "detail": "mode is access"}]},
                           {"index": 1, "parent": "router ospf 1", "global": False, "command_count": 1, "status": "PASS",
                            "issues": [], "current_config_found": False, "current_config": [],
                            "commands": [{"command": "router-id 10.0.0.1", "status": "not_available",
                                          "detail": "Command accepted for execution; semantic pre-validation is not available for this command."}]},
                           {"index": 2, "parent": None, "global": True, "command_count": 1, "status": "PASS", "issues": [],
                            "current_config_found": True, "current_config": [],
                            "commands": [{"command": "ip routing", "status": "verified", "detail": "line present in running configuration"}]}],
                "cli_preview": PREVIEW, "verification_commands": ["show vlan"]},
    "backup": {"job_id": "b0000000-0000-4000-8000-000000000009", "status": "success", "checksum": "9f2c" * 16, "storage_path": None},
    "approval": {"approval_id": "a0000000-0000-4000-8000-000000000009", "status": "pending"}}}

FLEET = {"total": 2, "healthy": 2, "degraded": 0, "down": 0, "unknown": 0, "last_updated": ago(seconds=20),
         "check_running": False, "devices": []}
# Overview widgets: latest telemetry per device, topology (managed + one unmanaged neighbor),
# one backup, one failure audit event.
TELEMETRY = {1: {"cpu_percent": 12.5, "memory_percent": 40.0, "uptime_seconds": 90061, "stale": False},
             12: {"cpu_percent": 93.0, "memory_percent": 61.0, "uptime_seconds": 3600, "stale": False}}
for i, t in TELEMETRY.items():
    t.update(device_id=i, hostname="x", status="ok", memory_used_mb=None, memory_total_mb=None, collected_at=ago(seconds=30),
             last_success_at=ago(seconds=30), error=None, interval_seconds=60, stale_after_seconds=180)
TOPOLOGY = {"last_discovery_at": ago(minutes=3), "last_attempt_at": ago(minutes=3), "managed_devices": 2, "active_links": 1,
            "unmanaged_neighbors": 1, "failing_devices": 0, "down_devices": 0, "discovery_running": False, "last_run": None,
            "nodes": [{"id": f"d{d['id']}", "device_id": d["id"], "hostname": d["hostname"], "management_ip": d["management_ip"],
                       "platform": d["platform"], "managed": True, "health_status": "healthy", "response_time_ms": 900,
                       "neighbor_count": 1, "topology_last_seen_at": ago(minutes=3)} for d in DEVICES]
                     + [{"id": "u1", "device_id": None, "hostname": "lab-ap", "management_ip": None, "platform": None, "managed": False,
                         "health_status": "unknown", "response_time_ms": None, "neighbor_count": 1, "topology_last_seen_at": ago(minutes=3)}],
            "links": [{"id": "l1", "source": "d12", "target": "d1", "source_interface": "ethernet1/1/1", "target_interface": "Te1/0/1",
                       "protocol": "lldp", "first_seen_at": ago(days=1), "last_seen_at": ago(minutes=3), "active": True,
                       "observed_bidirectionally": True, "relationship": "managed"}]}
BACKUPS = [{"id": 7, "job_id": None, "device_id": 1, "backup_type": "running-config", "job_type": "manual_backup",
            "storage_path": "/backups/Kenda-HARO-IDF-A/x.cfg", "checksum": "ab" * 32, "created_at": ago(hours=2),
            "hostname": "Kenda-HARO-IDF-A", "file_available": True}]
AUDIT = [{"id": 1, "job_id": None, "device_id": 12, "event_type": "apply_block_failed",
          "message": "Block 2 failed: % Error: VLAN 50 does not exist.", "created_at": ago(minutes=2)},
         {"id": 2, "job_id": None, "device_id": 1, "event_type": "precheck_succeeded", "message": "ok", "created_at": ago(minutes=1)}]

ROUTES = {"/api/v1/devices": DEVICES, "/api/v1/health/devices": FLEET, "/api/v1/approvals": APPROVALS, "/api/v1/jobs": [],
          "/api/v1/backups": BACKUPS, "/api/v1/audit": AUDIT, "/api/v1/topology": TOPOLOGY}
RECEIVED = []
GETS = []      # API GET paths, in order (GET /__gets)
FAILING = set()  # API paths that answer 500 (POST /__fail {"path": ...}; POST /__reset)


# ---- Interface Monitoring (Kenda-Core-1, id 12) ---------------------------------------------------
def iface(id_, name, status="up", admin="up", oper="up", mode="access", vlan=10, rx=None, tx=None, errors=0, crc=0,
          discards=0, erroring=False, role=None, description=None, speed=10**10, changed=None, **kw):
    item = {"id": id_, "name": name, "canonical_name": name.lower().replace(" ", ""), "description": description,
            "type": "port_channel" if name.startswith("Port") else "ethernet", "role": role, "admin_status": admin,
            "oper_status": oper, "status": status, "speed_bps": speed, "duplex": "full", "mode": mode,
            "access_vlan": vlan if mode == "access" else None, "native_vlan": 1 if mode == "trunk" else None,
            "allowed_vlans": "10-20" if mode == "trunk" else None, "port_channel": None, "mtu": 9216,
            "last_state_change": changed, "rx_bytes": 812345678901, "tx_bytes": 712345678901, "rx_packets": 912345678,
            "tx_packets": 812345678, "rx_errors": 3, "tx_errors": 0, "crc_errors": 17, "input_discards": 2,
            "output_discards": 9, "rx_utilization_pct": rx, "tx_utilization_pct": tx, "errors_delta": errors,
            "crc_delta": crc, "discards_delta": discards, "erroring": erroring}
    item.update(kw)
    return item


IFACES = [
    iface(1, "Eth 1/1/1", description="SERVER-A", rx=12.5, tx=3.25, changed=ago(days=8)),
    iface(2, "Eth 1/1/2", description="PRINTER", rx=0.5, tx=0.25, vlan=20),
    iface(3, "Eth 1/1/3", status="down", oper="down", description="SPARE-DOWN", rx=None, tx=None, changed=ago(minutes=12)),
    iface(4, "Eth 1/1/4", status="admin_down", admin="down", oper="down", description="SHUT", rx=None, tx=None),
    iface(5, "Eth 1/1/25", mode="trunk", description="TO-kenda-core-02", rx=71.2, tx=45.0, errors=4, crc=2, discards=9,
          erroring=True, role="inter_switch", speed=10**11),
    iface(6, "Eth 1/1/26", mode="trunk", description="UPLINK-WAN", rx=95.5, tx=10.0, role="uplink"),
    iface(7, "Port-channel 10", mode="trunk", description="LAG-ARRAY", rx=None, tx=None, role="lag", speed=None),
]


def summary(items):
    out = {"total": len(items), "up": 0, "down": 0, "admin_down": 0, "unknown": 0, "erroring": 0, "trunks": 0, "access_ports": 0}
    for i in items:
        out[i["status"]] += 1
        out["erroring"] += i["erroring"]
        out["trunks"] += i["mode"] == "trunk"
        out["access_ports"] += i["mode"] == "access"
    return out


IFACE_MODE = {"mode": "fresh"}

# Health evidence: POST /__health_mode {"mode": "degraded"} makes Kenda-Core-1 a device that
# answers ping but has no SSH management (the Kenda-HQ-SW-01 case); /__reset restores it.
SSH_UNAVAILABLE = {"health_status": "degraded", "icmp_reachable": True, "tcp_reachable": False, "ssh_reachable": False,
                   "cli_reachable": False, "health_reason": "ssh_management_unavailable",
                   "last_error": "Reachable by ICMP, SSH management unavailable (TCP 22 unreachable: timeout)"}
HEALTHY_CORE = {k: DEVICES[1].get(k) for k in SSH_UNAVAILABLE}
HEALTHY_CORE.update(icmp_reachable=True, health_reason="ok")


def set_health_mode(mode):
    values = SSH_UNAVAILABLE if mode == "degraded" else HEALTHY_CORE
    DEVICES[1].update(values)
    DETAIL["health"].update({("status" if k == "health_status" else k): v for k, v in values.items()})


def interfaces_response():
    mode = IFACE_MODE["mode"]
    base = {"device_id": 12, "hostname": "Kenda-Core-1", "platform": "dell_os10", "source": "cache",
            "stale_after_seconds": 660, "poll_interval_seconds": 300, "problems": [], "last_attempt": {"status": "success", "at": ago(minutes=2)}}
    if mode == "empty":
        return {**base, "status": "not_collected", "collected_at": None, "age_seconds": None, "stale": False,
                "collection_status": None, "summary": None, "interfaces": [],
                "last_attempt": {"status": "skipped_change", "reason": "change_in_progress", "at": ago(minutes=1)}}
    if mode == "large":  # a 52-port switch, for render timing
        items = [iface(100 + n, f"Eth 1/1/{n}", description=f"PORT-{n}", rx=round(n * 1.7 % 97, 2), tx=round(n * 0.9 % 60, 2),
                       mode="trunk" if n % 4 == 0 else "access", status="down" if n % 13 == 0 else "up",
                       oper="down" if n % 13 == 0 else "up", erroring=n % 17 == 0, errors=1 if n % 17 == 0 else 0)
                 for n in range(1, 53)]
        return {**base, "status": "ok", "collected_at": ago(minutes=1), "age_seconds": 60, "stale": False,
                "collection_status": "success", "summary": summary(items), "interfaces": items}
    stale = mode == "stale"
    collected = ago(minutes=14) if stale else ago(minutes=2)
    return {**base, "status": "stale" if stale else "ok", "collected_at": collected, "age_seconds": 840 if stale else 120,
            "stale": stale, "collection_status": "partial" if mode == "partial" else "success",
            "problems": ["'show interface status': CLI error (unsupported command?)"] if mode == "partial" else [],
            "last_attempt": {"status": "failed", "error": "Device unreachable (TCP 22)", "at": ago(minutes=1)} if stale else base["last_attempt"],
            "summary": summary(IFACES), "interfaces": IFACES}


def interface_history(interface_id):
    if interface_id != 5:
        return {"device_id": 12, "interface_id": interface_id, "range": "24h", "from": ago(hours=24), "to": ago(seconds=0),
                "interval_seconds": 300, "truncated": False, "samples": []}
    samples = [{"collected_at": ago(minutes=5 * n), "rx_utilization_pct": 40 + n % 7, "tx_utilization_pct": 20 + n % 3,
                "errors_delta": n % 4, "crc_delta": n % 2, "discards_delta": n % 5} for n in range(36, 0, -1)]
    return {"device_id": 12, "interface_id": 5, "range": "24h", "from": ago(hours=24), "to": ago(seconds=0),
            "interval_seconds": 300, "truncated": False, "samples": samples}


DETAIL = {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "192.0.2.10", "platform": "dell_os10", "enabled": True,
          "site": None, "role": None,
          "health": {"status": "healthy", "last_check_at": ago(seconds=20), "last_success_at": ago(seconds=20),
                     "response_time_ms": 900, "tcp_reachable": True, "ssh_reachable": True, "cli_reachable": True, "last_error": None},
          "operation": {"operation_state": "normal", "active_change_id": None, "change_started_at": None,
                        "polling_suppressed": False, "current_operation": None, "health_grace": False},
          "telemetry": {"device_id": 12, "hostname": "Kenda-Core-1", "status": "success", **{k: TELEMETRY[12][k] for k in (
              "cpu_percent", "memory_percent", "memory_used_mb", "memory_total_mb", "uptime_seconds", "collected_at",
              "last_success_at", "stale", "error", "interval_seconds", "stale_after_seconds")}},
          "latest_backup": None}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIST, **k)

    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/__requests":
            return self.send_json(RECEIVED)
        if path == "/__gets":
            return self.send_json(GETS)
        if path.startswith("/api/"):
            GETS.append(path)
            if path in FAILING:
                return self.send_json({"detail": "simulated failure"}, 500)
            if path == "/api/v1/devices/12":
                return self.send_json(DETAIL)
            if path == "/api/v1/devices/12/metrics":
                return self.send_json({"device_id": 12, "range": "24h", "interval_seconds": 60, "bucket_seconds": None,
                                       "downsampled": False, "samples": []})
            if path == "/api/v1/devices/12/interfaces":
                if IFACE_MODE["mode"] == "error":
                    return self.send_json({"detail": "Interface data is not available"}, 503)
                return self.send_json(interfaces_response())
            if path.startswith("/api/v1/devices/12/interfaces/") and path.endswith("/metrics"):
                return self.send_json(interface_history(int(path.split("/")[6])))
            if path.endswith("/metrics/latest"):
                device_id = int(path.split("/")[4])
                return self.send_json(TELEMETRY[device_id]) if device_id in TELEMETRY else self.send_json({"detail": "x"}, 404)
            if path in ROUTES:
                return self.send_json(ROUTES[path])
            if path.startswith("/api/v1/changes/precheck/"):
                return self.send_json(PRECHECK)
            return self.send_json({"detail": "not mocked"}, 404)
        if path == "/" or not os.path.exists(os.path.join(DIST, path.lstrip("/"))):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/__fail":
            FAILING.add(body["path"])
            return self.send_json({})
        if path == "/__reset":
            FAILING.clear()
            GETS.clear()
            IFACE_MODE["mode"] = "fresh"
            set_health_mode("healthy")
            return self.send_json({})
        if path == "/__health_mode":
            set_health_mode(body["mode"])
            return self.send_json({})
        if path == "/__iface_mode":
            IFACE_MODE["mode"] = body["mode"]
            return self.send_json({})
        if path == "/api/v1/changes/precheck":
            RECEIVED.append(body)
            return self.send_json({"request_id": PRECHECK["request_id"], "state": "queued", "device_id": body.get("device_id"),
                                   "hostname": "Kenda-Core-1", "platform": "dell_os10"}, 202)
        return self.send_json({"detail": "not mocked"}, 404)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Handler).serve_forever()
