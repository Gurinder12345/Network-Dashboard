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


def approval(status, device_id=12):
    return {"id": APPROVAL_ID, "device_id": device_id, "backup_job_id": str(uuid.uuid4()), "requested_by": "system",
            "approved_by": "approver" if status != "pending" else None, "status": status,
            "config_lines": OS10_CHANGE["config_lines"], "config_parents": OS10_CHANGE["config_parents"]}


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
