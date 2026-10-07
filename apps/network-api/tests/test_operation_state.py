"""
Optional device-operation metadata on the health APIs (device coordination, written by the
worker): operation_state / active_change_id / change_started_at / polling_suppressed /
current_operation / health_grace. Health status itself is never changed by it. Redis and
PostgreSQL are mocked.
    python -m unittest tests.test_operation_state -v
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import cache, health  # noqa: E402
from app.main import app  # noqa: E402

DEVICES = [{"id": 11, "hostname": "Kenda-Core-1", "management_ip": "192.0.2.10", "platform": "dell_os10", "enabled": True},
           {"id": 12, "hostname": "Kenda-Core-2", "management_ip": "192.0.2.20", "platform": "dell_os10", "enabled": True}]
HEALTHY = {"status": "healthy", "last_check_at": "2026-10-07T12:00:00+00:00", "last_success_at": "2026-10-07T12:00:00+00:00",
           "response_time_ms": 900, "tcp_reachable": True, "ssh_reachable": True, "cli_reachable": True, "last_error": None,
           "consecutive_failures": 0, "last_status_change_at": None}


class OperationStateTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.redis = {
            "health:device:11": json.dumps({"device_id": 11, **HEALTHY}),
            "health:device:12": json.dumps({"device_id": 12, **HEALTHY}),
            "device:11:change-in-progress": json.dumps({"token": "t", "change_id": "chg-184", "job_id": "j",
                                                        "started_at": "2026-10-07T12:01:00+00:00"}),
            "device:11:operation": json.dumps({"operation": "apply", "token": "t"}),
            "device:11:health-grace": "1791370000.0",
            "device:12:operation": json.dumps({"operation": "telemetry", "token": "u"}),
        }
        mock.patch.object(cache, "mget_json", side_effect=lambda keys: [json.loads(self.redis[k]) if k in self.redis else None
                                                                        for k in keys]).start()
        mock.patch.object(cache, "get_json", side_effect=lambda k: DEVICES if k == health.DEVICES_LIST_KEY else None).start()
        mock.patch.object(cache, "set_json").start()
        mock.patch.object(cache, "exists", return_value=False).start()
        self.client = TestClient(app)

    def test_fleet_health_carries_change_state_without_changing_status(self):
        body = self.client.get("/api/v1/health/devices").json()
        core1, core2 = body["devices"]
        self.assertEqual((core1["status"], core1["operation_state"], core1["active_change_id"]),
                         ("healthy", "change_in_progress", "chg-184"))
        self.assertEqual((core1["change_started_at"], core1["polling_suppressed"], core1["current_operation"], core1["health_grace"]),
                         ("2026-10-07T12:01:00+00:00", True, "apply", True))
        self.assertEqual((core2["operation_state"], core2["polling_suppressed"], core2["current_operation"], core2["health_grace"]),
                         ("normal", False, "telemetry", False))
        self.assertEqual((body["healthy"], body["degraded"]), (2, 0))  # an active change is not a fault

    def test_devices_list_and_single_device(self):
        devices = {d["id"]: d for d in self.client.get("/api/v1/devices").json()}
        self.assertEqual((devices[11]["health_status"], devices[11]["operation_state"]), ("healthy", "change_in_progress"))
        self.assertEqual(self.client.get("/api/v1/health/devices/12").json()["operation_state"], "normal")

    def test_redis_unavailable_means_normal_not_guessed(self):
        self.assertEqual(health.operation_states([11])[11]["operation_state"], "change_in_progress")
        mock.patch.object(cache, "mget_json", side_effect=lambda keys: [None] * len(keys)).start()
        self.assertEqual(health.operation_states([11, 12]), {i: health.NORMAL_OPERATION for i in (11, 12)})
        self.assertEqual(health.operation_states([]), {})


if __name__ == "__main__":
    unittest.main()
