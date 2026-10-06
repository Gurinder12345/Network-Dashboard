"""
Local mock API + static server for the UI browser tests (synthetic data only; never
contacts a device or the real API). Serves the production build (dist/) and the API
routes the Changes and Approvals pages use. Records POSTed precheck bodies at
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
ROUTES = {"/api/v1/devices": DEVICES, "/api/v1/health/devices": FLEET, "/api/v1/approvals": APPROVALS, "/api/v1/jobs": [],
          "/api/v1/backups": [], "/api/v1/audit": []}
RECEIVED = []


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
        if path.startswith("/api/"):
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
        if path == "/api/v1/changes/precheck":
            RECEIVED.append(body)
            return self.send_json({"request_id": PRECHECK["request_id"], "state": "queued", "device_id": body.get("device_id"),
                                   "hostname": "Kenda-Core-1", "platform": "dell_os10"}, 202)
        return self.send_json({"detail": "not mocked"}, 404)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Handler).serve_forever()
