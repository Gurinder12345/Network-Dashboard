"""
Change APIs for Dell OS6 + Dell OS10: generic precheck route (platform dispatch in the
worker), legacy OS6 route, approve/apply/cancel for both platforms. DB and Celery mocked.
    python -m unittest tests.test_changes_api -v
"""

import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import approval_actions, changes  # noqa: E402
from app.main import app  # noqa: E402

DEVICES = {
    1: {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "192.0.2.31", "platform": "dell_os6", "enabled": True},
    12: {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "192.0.2.10", "platform": "dell_os10", "enabled": True},
    7: {"id": 7, "hostname": "lab-disabled", "management_ip": "192.0.2.99", "platform": "dell_os10", "enabled": False},
    9: {"id": 9, "hostname": "other", "management_ip": "192.0.2.50", "platform": "cisco_ios", "enabled": True},
}
OS10_CHANGE = {"device_id": 12, "config_parents": ["interface ethernet1/1/5"], "config_lines": ["description AUTOMATION-TEST"]}
APPROVAL_ID = str(uuid.uuid4())


def approval(status, device_id=12, execution_result=None):
    return {"id": APPROVAL_ID, "device_id": device_id, "backup_job_id": str(uuid.uuid4()), "requested_by": "system",
            "approved_by": "approver" if status != "pending" else None, "status": status,
            "config_lines": OS10_CHANGE["config_lines"], "config_parents": OS10_CHANGE["config_parents"],
            "config_blocks": [{"order": 0, "parent": "interface ethernet1/1/5", "commands": ["description AUTOMATION-TEST"]}],
            "block_count": 1, "command_count": 1, "execution_result": execution_result}


MULTI = {"device_id": 12, "blocks": [
    {"parent": "interface ethernet 1/1/18", "commands": ["description APP-SERVER", "switchport mode trunk"]},
    {"parent": "vlan 50", "commands": ["name USERS"]},
    {"parent": None, "commands": ["ip routing"]},
    {"parent": "", "commands": ["router ospf 1", "router-id 10.0.0.1"]},
], "verification_commands": ["show vlan"]}


class PrecheckRouteTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(changes, "get_device_by_id", side_effect=DEVICES.get).start()
        self.audit = mock.patch.object(changes, "create_audit_event").start()
        self.send = mock.patch.object(changes.celery_app, "send_task").start()
        self.send.return_value.id = str(uuid.uuid4())
        self.client = TestClient(app)

    def test_generic_route_accepts_os10_and_os6(self):
        for device_id, platform in ((12, "dell_os10"), (1, "dell_os6")):
            with self.subTest(platform=platform):
                r = self.client.post("/api/v1/changes/precheck", json={**OS10_CHANGE, "device_id": device_id})
                self.assertEqual(r.status_code, 202, r.text)
                self.assertEqual(r.json()["platform"], platform)
                self.assertEqual(self.send.call_args.args[0], "network_worker.change_precheck")
                self.assertEqual(self.send.call_args.kwargs["kwargs"]["target_host"], DEVICES[device_id]["hostname"])
        self.assertIn("Dell OS6 precheck requested", self.audit.call_args.kwargs["message"])

    def test_legacy_route_is_os6_only(self):
        r = self.client.post("/api/v1/changes/os6/precheck", json={**OS10_CHANGE, "device_id": 1})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.send.call_args.args[0], "network_worker.os6_change_precheck")
        self.assertEqual(self.client.post("/api/v1/changes/os6/precheck", json=OS10_CHANGE).status_code, 422)

    def test_rejections(self):
        self.assertEqual(self.client.post("/api/v1/changes/precheck", json={**OS10_CHANGE, "device_id": 9}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/changes/precheck", json={**OS10_CHANGE, "device_id": 7}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/changes/precheck", json={**OS10_CHANGE, "device_id": 404}).status_code, 404)
        for bad in (["configure terminal"], ["end"], ["description a\nshutdown"], []):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post("/api/v1/changes/precheck", json={**OS10_CHANGE, "config_lines": bad}).status_code, 422)
        self.send.assert_not_called()

    def test_status_route_on_both_paths(self):
        with mock.patch.object(changes, "AsyncResult") as result:
            result.return_value.state = "FAILURE"
            result.return_value.result = ValueError("Rejected by OS10 change policy: 'shutdown': shutdown / no shutdown is not allowed")
            rid = str(uuid.uuid4())
            for path in (f"/api/v1/changes/precheck/{rid}", f"/api/v1/changes/os6/precheck/{rid}"):
                body = self.client.get(path).json()
                self.assertEqual(body["state"], "failed")
                self.assertIn("Rejected by OS10 change policy", body["error"])
            result.return_value.state = "SUCCESS"
            result.return_value.result = {"status": "pending_approval", "platform": "dell_os10", "target_host": "Kenda-Core-1",
                                          "dry_run": {"would_change": True}, "approval": {"approval_id": "a", "status": "pending"}}
            body = self.client.get(f"/api/v1/changes/precheck/{rid}").json()
            self.assertEqual((body["result"]["platform"], body["result"]["approval"]["status"]), ("dell_os10", "pending"))

    def test_blocks_are_queued_in_order(self):
        r = self.client.post("/api/v1/changes/precheck", json=MULTI)
        self.assertEqual(r.status_code, 202, r.text)
        kwargs = self.send.call_args.kwargs["kwargs"]
        self.assertEqual(kwargs["config_blocks"], [
            {"order": 0, "parent": "interface ethernet 1/1/18", "commands": ["description APP-SERVER", "switchport mode trunk"]},
            {"order": 1, "parent": "vlan 50", "commands": ["name USERS"]},
            {"order": 2, "parent": None, "commands": ["ip routing"]},
            {"order": 3, "parent": None, "commands": ["router ospf 1", "router-id 10.0.0.1"]}])
        self.assertEqual(kwargs["verification_commands"], ["show vlan"])
        self.assertNotIn("config_lines", kwargs)
        self.assertIn("(request_id=", self.audit.call_args.kwargs["message"])
        self.assertIn("4 block(s), 6 command(s)", self.audit.call_args.kwargs["message"])

    def test_client_order_is_ignored_and_legacy_form_becomes_one_block(self):
        body = {"device_id": 12, "blocks": [{"order": 9, "parent": "vlan 60", "commands": ["name B"]},
                                            {"order": 1, "parent": "vlan 50", "commands": ["name A"]}]}
        self.client.post("/api/v1/changes/precheck", json=body)
        self.assertEqual([b["parent"] for b in self.send.call_args.kwargs["kwargs"]["config_blocks"]], ["vlan 60", "vlan 50"])
        self.client.post("/api/v1/changes/precheck", json=OS10_CHANGE)
        self.assertEqual(self.send.call_args.kwargs["kwargs"]["config_blocks"],
                         [{"order": 0, "parent": "interface ethernet1/1/5", "commands": ["description AUTOMATION-TEST"]}])

    def test_no_command_category_is_rejected(self):
        commands = ["switchport mode trunk", "shutdown", "spanning-tree mode rstp", "router ospf 1", "router bgp 65000",
                    "ip route 0.0.0.0/0 192.0.2.1", "aaa authentication login default local", "snmp-server community lab ro",
                    "access-list 10 permit any", "username netops password x role sysadmin", "no switchport", "reload"]
        for device_id in (12, 1):
            with self.subTest(device_id=device_id):
                r = self.client.post("/api/v1/changes/precheck", json={"device_id": device_id, "blocks": [
                    {"parent": None, "commands": commands}, {"parent": "interface mgmt1/1/1", "commands": ["ip address dhcp"]}]})
                self.assertEqual(r.status_code, 202, r.text)
                self.assertEqual(self.send.call_args.kwargs["kwargs"]["config_blocks"][0]["commands"], commands)

    def test_malformed_requests(self):
        cases = {
            "no blocks": {"device_id": 12}, "zero blocks": {"device_id": 12, "blocks": []},
            "zero commands": {"device_id": 12, "blocks": [{"parent": "vlan 5", "commands": []}]},
            "missing commands": {"device_id": 12, "blocks": [{"parent": "vlan 5"}]},
            "non-string command": {"device_id": 12, "blocks": [{"parent": None, "commands": [42]}]},
            "object command": {"device_id": 12, "blocks": [{"parent": None, "commands": [{"cli": "x"}]}]},
            "non-string parent": {"device_id": 12, "blocks": [{"parent": ["x"], "commands": ["x"]}]},
            "null byte": {"device_id": 12, "blocks": [{"parent": None, "commands": ["descr\u0000iption"]}]},
            "newline": {"device_id": 12, "blocks": [{"parent": None, "commands": ["description a\nreload"]}]},
            "escape": {"device_id": 12, "blocks": [{"parent": None, "commands": ["\u001b[2J"]}]},
            "mode command": {"device_id": 12, "blocks": [{"parent": None, "commands": ["end"]}]},
            "block not object": {"device_id": 12, "blocks": ["shutdown"]},
            "unknown block field": {"device_id": 12, "blocks": [{"parent": None, "commands": ["x"], "x": 1}]},
            "unknown top field": {"device_id": 12, "blocks": [{"parent": None, "commands": ["x"]}], "dry_run": False},
            "both forms": {**OS10_CHANGE, "blocks": [{"parent": None, "commands": ["x"]}]},
            "write verification": {"device_id": 12, "blocks": [{"parent": None, "commands": ["x"]}],
                                   "verification_commands": ["reload"]},
            "blocks not list": {"device_id": 12, "blocks": {"parent": None}},
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual(self.client.post("/api/v1/changes/precheck", json=body).status_code, 422)
        r = self.client.post("/api/v1/changes/precheck", content=b'{"device_id": 12, "blocks": [', headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 422)
        self.send.assert_not_called()

    def test_resource_limits(self):
        one = {"parent": None, "commands": ["x"]}
        cases = {"blocks": [one] * 51, "commands": [{"parent": None, "commands": ["x"] * 101}],
                 "total": [{"parent": None, "commands": ["x"] * 100}] * 6,
                 "length": [{"parent": None, "commands": ["x" * 1001]}], "parent": [{"parent": "p" * 1001, "commands": ["x"]}]}
        for name, blocks in cases.items():
            with self.subTest(name):
                r = self.client.post("/api/v1/changes/precheck", json={"device_id": 12, "blocks": blocks})
                self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.post("/api/v1/changes/precheck", json={"device_id": 12, "blocks": [
            {"parent": None, "commands": ["x" * 1000] * 100}] * 5}).status_code, 202)

    def test_multi_block_precheck_result_is_forwarded(self):
        dry_run = {"would_change": True, "verification_method": "running-configuration", "already_present": [],
                   "proposed_changes": ["shutdown"], "command_results": [], "overall": "FAILED",
                   "checks": {"structural_validation": "PASS", "device_connectivity": "PASS",
                              "current_config_capture": "PASS", "semantic_verification": "PARTIAL"},
                   "blocks": [{"index": 0, "status": "PASS"}, {"index": 1, "status": "FAILED"}],
                   "cli_preview": "! Block 1\nip routing", "verification_commands": ["show vlan"],
                   "raw_device_output": "never forwarded"}
        with mock.patch.object(changes, "AsyncResult") as result:
            result.return_value.state = "SUCCESS"
            result.return_value.result = {"status": "precheck_failed", "platform": "dell_os10", "target_host": "Kenda-Core-1",
                                          "rejection_reasons": ["Block 2: x"], "dry_run": dry_run, "block_count": 2,
                                          "command_count": 3}
            body = self.client.get(f"/api/v1/changes/precheck/{uuid.uuid4()}").json()["result"]
        self.assertEqual((body["status"], body["rejection_reasons"], body["block_count"], body["command_count"]),
                         ("precheck_failed", ["Block 2: x"], 2, 3))
        for key in ("overall", "checks", "blocks", "cli_preview", "verification_commands", "proposed_changes"):
            self.assertEqual(body["dry_run"][key], dry_run[key])
        self.assertNotIn("raw_device_output", body["dry_run"])
        self.assertIsNone(body["approval"])


class CompatibilityAdapterTests(unittest.TestCase):
    def row(self, config_blocks, lines, parents):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        return (APPROVAL_ID, 12, None, "system", None, "applied", lines, parents, now, None, None, None, None,
                config_blocks, None, None, None)

    def test_historical_single_parent_row_is_one_block(self):
        from app.db.approvals import _row_to_approval
        view = _row_to_approval(self.row(None, ["description X", "shutdown"], ["interface Tw1/0/3"]))
        self.assertEqual(view["config_blocks"], [{"order": 0, "parent": "interface Tw1/0/3", "commands": ["description X", "shutdown"]}])
        self.assertEqual((view["legacy_single_block"], view["block_count"], view["command_count"]), (True, 1, 2))
        self.assertEqual(view["cli_preview"], "! Block 1\ninterface Tw1/0/3\n description X\n shutdown")
        self.assertEqual((view["config_lines"], view["verification_commands"], view["execution_result"]),
                         (["description X", "shutdown"], [], None))
        global_view = _row_to_approval(self.row(None, ["vlan 200"], None))
        self.assertEqual(global_view["cli_preview"], "! Block 1 - Global\nvlan 200")

    def test_new_rows_use_config_blocks(self):
        from app.db.approvals import _row_to_approval
        blocks = [{"order": 0, "parent": "vlan 50", "commands": ["name USERS"]}, {"order": 1, "parent": None, "commands": ["ip routing"]}]
        view = _row_to_approval(self.row(blocks, ["vlan 50", "name USERS", "ip routing"], None))
        self.assertEqual((view["config_blocks"], view["legacy_single_block"], view["block_count"], view["command_count"]),
                         (blocks, False, 2, 2))


class ApprovalRouteTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(approval_actions, "get_device_by_id", side_effect=DEVICES.get).start()
        self.get = mock.patch.object(approval_actions, "get_approval").start()
        self.approve = mock.patch.object(approval_actions, "approve_pending_approval").start()
        self.claim = mock.patch.object(approval_actions, "claim_approval_for_apply").start()
        self.release = mock.patch.object(approval_actions, "release_apply_claim").start()
        self.cancel = mock.patch.object(approval_actions, "cancel_open_approval").start()
        self.audit = mock.patch.object(approval_actions, "create_audit_event").start()
        self.send = mock.patch.object(approval_actions.celery_app, "send_task").start()
        self.send.return_value.id = str(uuid.uuid4())
        self.client = TestClient(app)

    def test_os10_approve_and_apply_use_the_shared_task(self):
        self.get.return_value = approval("pending")
        self.approve.return_value = approval("approved")
        r = self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/approve", json={"approved_by": "reviewer"})
        self.assertEqual(r.status_code, 200, r.text)
        self.get.return_value = approval("approved")
        self.claim.return_value = approval("applying")
        r = self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/apply")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.send.call_args.args[0], "network_worker.apply_approved_change")
        self.assertEqual(self.send.call_args.kwargs["kwargs"], {"approval_id": APPROVAL_ID, "claimed_by_api": True})

    def test_apply_status_includes_block_progress(self):
        progress = {"outcome": "partial_apply", "blocks": [{"index": 0, "status": "applied"}, {"index": 1, "status": "failed"}]}
        self.get.return_value = approval("failed", execution_result=progress)
        with mock.patch.object(approval_actions, "AsyncResult") as result:
            result.return_value.state = "FAILURE"
            result.return_value.result = RuntimeError("PARTIAL APPLY on Kenda-Core-1")
            body = self.client.get(f"/api/v1/approvals/{APPROVAL_ID}/apply/{uuid.uuid4()}").json()
        self.assertEqual((body["approval_status"], body["execution_result"]), ("failed", progress))
        self.assertIn("PARTIAL APPLY", body["error"])

    def test_apply_is_claimed_once(self):
        self.get.return_value = approval("approved")
        self.claim.return_value = None  # another request won the approved -> applying UPDATE
        self.assertEqual(self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/apply").status_code, 409)
        self.send.assert_not_called()

    def test_wrong_states_and_platforms(self):
        for status in ("pending", "applying", "applied", "failed", "cancelled"):
            with self.subTest(status=status):
                self.get.return_value = approval(status)
                self.assertEqual(self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/apply").status_code, 409)
        self.get.return_value = approval("approved", device_id=9)
        self.assertEqual(self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/apply").status_code, 422)
        self.get.return_value = approval("approved", device_id=7)
        self.assertEqual(self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/apply").status_code, 422)
        self.claim.assert_not_called()

    def test_enqueue_failure_releases_the_claim(self):
        self.get.return_value = approval("approved")
        self.claim.return_value = approval("applying")
        self.send.side_effect = ConnectionError("broker down")
        self.assertEqual(self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/apply").status_code, 503)
        self.release.assert_called_once_with(APPROVAL_ID)

    def test_os10_cancellation_rules(self):
        body = {"cancelled_by": "operator", "reason": "not needed"}
        for status in ("pending", "approved"):
            with self.subTest(status=status):
                self.get.return_value = approval(status)
                self.cancel.return_value = approval("cancelled")
                self.assertEqual(self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/cancel", json=body).status_code, 200)
        for status in ("applying", "applied", "failed", "cancelled"):
            with self.subTest(status=status):
                self.get.return_value = approval(status)
                self.assertEqual(self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/cancel", json=body).status_code, 409)
        # Apply claimed it between the read and the cancel UPDATE: the UPDATE matches nothing.
        self.get.side_effect = [approval("approved"), approval("applying")]
        self.cancel.return_value = None
        r = self.client.post(f"/api/v1/approvals/{APPROVAL_ID}/cancel", json=body)
        self.assertEqual(r.status_code, 409)
        self.assertIn("applying", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
