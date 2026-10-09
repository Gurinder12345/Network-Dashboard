"""
Health evidence in the API: icmp_reachable / health_reason pass through from Redis and
PostgreSQL, rows cached by an older worker keep the same shape, and the API keeps serving
the original columns if migration 010 is not applied yet.

    python -m unittest tests.test_health_evidence -v
HEALTH_TEST_PG_DSN (disposable PostgreSQL with `devices`) adds the real-SQL fallback test.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import cache, device_detail, health  # noqa: E402
from app.db import health as db_health  # noqa: E402
from app.main import app  # noqa: E402

DEVICES = [{"id": 3, "hostname": "Kenda-HQ-SW-01", "management_ip": "10.0.0.3", "platform": "dell_os6", "enabled": True}]
ROW = {"device_id": 3, "status": "degraded", "last_check_at": "2026-10-09T12:00:00+00:00", "last_success_at": None,
       "response_time_ms": 3100, "icmp_reachable": True, "tcp_reachable": False, "ssh_reachable": False,
       "cli_reachable": False, "last_error": "Reachable by ICMP, SSH management unavailable (TCP 22 unreachable: timeout)",
       "health_reason": "ssh_management_unavailable", "consecutive_failures": 4, "last_status_change_at": None}


class ApiEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.client = TestClient(app)
        mock.patch.object(health, "cached_devices", return_value=DEVICES).start()
        mock.patch.object(health, "operation_states", return_value={}).start()
        self.mget = mock.patch.object(cache, "mget_json", return_value=[ROW]).start()
        mock.patch.object(cache, "get_json", return_value=None).start()
        mock.patch.object(cache, "set_json").start()
        mock.patch.object(cache, "exists", return_value=False).start()
        self.rows = mock.patch.object(health, "get_health_rows", return_value={}).start()

    def test_devices_list_carries_reason_and_icmp(self):
        device = self.client.get("/api/v1/devices").json()[0]
        self.assertEqual((device["health_status"], device["health_reason"], device["icmp_reachable"], device["tcp_reachable"]),
                         ("degraded", "ssh_management_unavailable", True, False))

    def test_fleet_health_entries_carry_reason(self):
        entry = self.client.get("/api/v1/health/devices").json()["devices"][0]
        self.assertEqual((entry["status"], entry["health_reason"], entry["icmp_reachable"]), ("degraded", "ssh_management_unavailable", True))

    def test_device_detail_health_block_in_one_request(self):
        mock.patch.object(device_detail, "get_device_by_id", return_value=DEVICES[0]).start()
        mock.patch.object(device_detail, "latest_telemetry", return_value={}).start()
        mock.patch.object(device_detail, "operation_states", return_value={}).start()
        mock.patch.object(device_detail, "latest_backup_for_device", return_value=None).start()
        body = self.client.get("/api/v1/devices/3").json()
        self.assertEqual(body["health"]["health_reason"], "ssh_management_unavailable")
        self.assertTrue(body["health"]["icmp_reachable"])

    def test_rows_cached_by_an_older_worker_keep_the_same_shape(self):
        old = {k: v for k, v in ROW.items() if k not in ("icmp_reachable", "health_reason")}
        self.mget.return_value = [old]
        device = self.client.get("/api/v1/devices").json()[0]
        self.assertIsNone(device["health_reason"])
        self.assertIsNone(device["icmp_reachable"])
        entry = self.client.get("/api/v1/health/devices").json()["devices"][0]
        self.assertIn("health_reason", entry)

    def test_unknown_device_has_evidence_keys(self):
        self.mget.return_value = [None]
        device = self.client.get("/api/v1/devices").json()[0]
        self.assertEqual((device["health_status"], device["health_reason"], device["icmp_reachable"]), ("unknown", None, None))


DSN = os.getenv("HEALTH_TEST_PG_DSN")


@unittest.skipUnless(DSN, "HEALTH_TEST_PG_DSN not set (disposable PostgreSQL)")
class DbFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from urllib.parse import urlparse

        from app.db import client

        url = urlparse(DSN)
        client.DB_HOST, client.DB_PORT, client.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
        client.DB_USER, client.DB_PASSWORD = url.username, url.password
        cls.conn = psycopg.connect(DSN, autocommit=True)
        migrations = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "db", "migrations")
        cls.sql = {n: open(os.path.join(migrations, n)).read() for n in
                   ("001_device_health.sql", "010_health_evidence.sql", "010_health_evidence.down.sql")}
        cls.conn.execute(cls.sql["001_device_health.sql"])
        cls.device = cls.conn.execute("SELECT id FROM devices ORDER BY id LIMIT 1").fetchone()[0]
        cls.conn.execute("DELETE FROM device_health WHERE device_id = %s", (cls.device,))
        cls.conn.execute("INSERT INTO device_health (device_id, status, tcp_reachable) VALUES (%s, 'degraded', false)", (cls.device,))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_before_and_after_migration_010(self):
        self.conn.execute(self.sql["010_health_evidence.down.sql"])
        row = db_health.get_health_rows([self.device])[self.device]
        self.assertEqual((row["status"], row["health_reason"], row["icmp_reachable"]), ("degraded", None, None))
        self.conn.execute(self.sql["010_health_evidence.sql"])
        self.conn.execute("UPDATE device_health SET icmp_reachable = true, health_reason = 'ssh_management_unavailable' "
                          "WHERE device_id = %s", (self.device,))
        row = db_health.get_health_rows([self.device])[self.device]
        self.assertEqual((row["health_reason"], row["icmp_reachable"]), ("ssh_management_unavailable", True))


if __name__ == "__main__":
    unittest.main()
