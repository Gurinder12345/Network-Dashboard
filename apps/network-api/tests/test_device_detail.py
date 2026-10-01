"""
Device detail + telemetry read APIs. Unit tests mock DB/Redis; the history SQL is tested
against a disposable PostgreSQL when TELEMETRY_TEST_PG_DSN is set. Run from apps/network-api:
    python -m unittest tests.test_device_detail -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import device_detail  # noqa: E402
from app.main import app  # noqa: E402

CORE = {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "10.0.0.10", "platform": "dell_os10", "enabled": True}
NOW = datetime.now(timezone.utc)


def latest(**overrides):
    return {"device_id": 12, "status": "success", "cpu_percent": 18.4, "memory_percent": 42.1,
            "memory_used_mb": 3400.0, "memory_total_mb": 8000.0, "uptime_seconds": 2095200,
            "collected_at": NOW.isoformat(), "last_success_at": NOW.isoformat(), "error": None, **overrides}


class DetailApiTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.client = TestClient(app)
        mock.patch.object(device_detail, "get_device_by_id", side_effect=lambda i: CORE if i == 12 else None).start()
        self.cache_get = mock.patch.object(device_detail.cache, "get_json", return_value=None).start()
        self.db_latest = mock.patch.object(device_detail, "latest_metrics", return_value=None).start()
        self.history = mock.patch.object(device_detail, "metric_history", return_value=[]).start()
        mock.patch.object(device_detail, "health_by_device", return_value={12: {
            "device_id": 12, "status": "healthy", "last_check_at": NOW.isoformat(), "last_success_at": NOW.isoformat(),
            "response_time_ms": 13100, "tcp_reachable": True, "ssh_reachable": True, "cli_reachable": True,
            "last_error": None, "consecutive_failures": 0, "last_status_change_at": None}}).start()
        self.backup = mock.patch.object(device_detail, "latest_backup_for_device", return_value=None).start()
        mock.patch.object(device_detail, "file_available", return_value=True).start()

    def test_device_detail(self):
        self.cache_get.return_value = latest()
        self.backup.return_value = {"id": 7, "storage_path": "/backups/Kenda-Core-1/20261001T080000Z.cfg",
                                    "created_at": NOW.isoformat(), "job_type": "manual_backup"}
        body = self.client.get("/api/v1/devices/12").json()

        self.assertEqual((body["hostname"], body["platform"], body["enabled"]), ("Kenda-Core-1", "dell_os10", True))
        self.assertEqual(body["health"]["status"], "healthy")
        self.assertEqual(body["health"]["response_time_ms"], 13100)
        self.assertEqual(body["telemetry"]["cpu_percent"], 18.4)
        self.assertEqual(body["latest_backup"], {"backup_id": 7, "created_at": NOW.isoformat(), "source": "manual_backup",
                                                 "filename": "Kenda-Core-1_20261001T080000Z.cfg", "file_available": True})
        for secret_field in ("credential_path", "password", "storage_path"):
            self.assertNotIn(secret_field, str(body))

    def test_unknown_device_404_everywhere(self):
        for path in ("/api/v1/devices/999", "/api/v1/devices/999/metrics/latest", "/api/v1/devices/999/metrics?range=1h"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_latest_from_cache_then_db(self):
        self.cache_get.return_value = latest()
        body = self.client.get("/api/v1/devices/12/metrics/latest").json()
        self.assertEqual((body["status"], body["stale"], body["memory_percent"]), ("success", False, 42.1))
        self.db_latest.assert_not_called()

        self.cache_get.return_value = None
        self.db_latest.return_value = latest(cpu_percent=5.0)
        self.assertEqual(self.client.get("/api/v1/devices/12/metrics/latest").json()["cpu_percent"], 5.0)

    def test_never_collected_has_no_fake_zeros(self):
        body = self.client.get("/api/v1/devices/12/metrics/latest").json()
        self.assertEqual(body["status"], "not_collected")
        self.assertFalse(body["stale"])
        for field in device_detail.METRIC_FIELDS:
            self.assertIsNone(body[field])

    def test_partial_sample(self):
        self.cache_get.return_value = latest(status="partial", cpu_percent=None, error="CPU unavailable")
        body = self.client.get("/api/v1/devices/12/metrics/latest").json()
        self.assertEqual((body["status"], body["cpu_percent"], body["memory_percent"], body["error"]),
                         ("partial", None, 42.1, "CPU unavailable"))

    def test_stale_and_failed_latest(self):
        old = (NOW - timedelta(seconds=device_detail.STALE_AFTER_SECONDS + 5)).isoformat()
        self.cache_get.return_value = latest(status="failed", cpu_percent=None, memory_percent=None,
                                             collected_at=NOW.isoformat(), last_success_at=old, error="Device unreachable (TCP 22)")
        body = self.client.get("/api/v1/devices/12/metrics/latest").json()
        self.assertTrue(body["stale"])
        self.assertEqual(body["error"], "Device unreachable (TCP 22)")

        fresh = (NOW - timedelta(seconds=device_detail.STALE_AFTER_SECONDS - 20)).isoformat()
        self.cache_get.return_value = latest(last_success_at=fresh)
        self.assertFalse(self.client.get("/api/v1/devices/12/metrics/latest").json()["stale"])

        # Failed every time so far: stale (there is no success at all).
        self.cache_get.return_value = latest(status="failed", cpu_percent=None, memory_percent=None, last_success_at=None)
        self.assertTrue(self.client.get("/api/v1/devices/12/metrics/latest").json()["stale"])

    def test_ranges_and_bounds(self):
        expected = {"1h": (3600, None), "6h": (21600, None), "24h": (86400, None), "7d": (604800, 600)}
        for name, (seconds, bucket) in expected.items():
            with self.subTest(range=name):
                body = self.client.get(f"/api/v1/devices/12/metrics?range={name}").json()
                self.assertEqual(self.history.call_args.args, (12, seconds, bucket))
                self.assertEqual((body["range"], body["bucket_seconds"], body["downsampled"]), (name, bucket, bucket is not None))

        self.client.get("/api/v1/devices/12/metrics")
        self.assertEqual(self.history.call_args.args, (12, 86400, None))  # default 24h

        for bad in ("30d", "1y", "0", "3600", "-1h", "all"):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.get(f"/api/v1/devices/12/metrics?range={bad}").status_code, 422)


DSN = os.getenv("TELEMETRY_TEST_PG_DSN")


@unittest.skipUnless(DSN, "TELEMETRY_TEST_PG_DSN not set (needs a disposable PostgreSQL)")
class HistorySqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        from app.db import client, metrics

        url = urlparse(DSN)
        client.DB_HOST, client.DB_PORT, client.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
        client.DB_USER, client.DB_PASSWORD = url.username, url.password
        cls.metrics = metrics
        migration = os.path.join(os.path.dirname(__file__), "..", "..", "..", "db", "migrations", "005_device_metrics.sql")
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS device_metrics")
            conn.execute(open(migration).read())
            device = conn.execute("SELECT id FROM devices ORDER BY id LIMIT 1").fetchone()[0]
            cls.device = device
            # 8 days of 1/min samples; every 10th minute of the last hour is a failed attempt.
            conn.execute(
                """
                INSERT INTO device_metrics (device_id, collected_at, cpu_percent, memory_percent, memory_used_mb,
                                            memory_total_mb, uptime_seconds, source, collection_status, error)
                SELECT %s, now() - make_interval(mins => m),
                       CASE WHEN m < 60 AND m %% 10 = 0 THEN NULL ELSE (m %% 50) END,
                       CASE WHEN m < 60 AND m %% 10 = 0 THEN NULL ELSE 40 END,
                       NULL, NULL, NULL, 'cli:test',
                       CASE WHEN m < 60 AND m %% 10 = 0 THEN 'failed' ELSE 'success' END,
                       CASE WHEN m < 60 AND m %% 10 = 0 THEN 'Device unreachable (TCP 22)' END
                FROM generate_series(0, 8 * 1440) AS m
                """,
                (device,),
            )

    def test_raw_ranges_are_bounded_and_ordered(self):
        for seconds, expected in ((3600, 60), (21600, 360), (86400, 1440)):
            samples = self.metrics.metric_history(self.device, seconds)
            with self.subTest(seconds=seconds):
                self.assertIn(len(samples), (expected, expected + 1))
                stamps = [s["collected_at"] for s in samples]
                self.assertEqual(stamps, sorted(stamps))

    def test_failed_attempts_are_gaps_not_zeros(self):
        samples = self.metrics.metric_history(self.device, 3600)
        failed = [s for s in samples if s["status"] == "failed"]
        self.assertGreaterEqual(len(failed), 5)
        self.assertTrue(all(s["cpu_percent"] is None and s["memory_percent"] is None for s in failed))

    def test_seven_day_buckets(self):
        samples = self.metrics.metric_history(self.device, 7 * 86400, 600)
        self.assertLessEqual(len(samples), 7 * 144 + 1)
        self.assertGreaterEqual(len(samples), 7 * 144 - 1)
        stamps = [datetime.fromisoformat(s["collected_at"]) for s in samples]
        self.assertTrue(all(int(t.timestamp()) % 600 == 0 for t in stamps))
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(s["memory_percent"] == 40.0 for s in samples if s["memory_percent"] is not None))

    def test_latest_metrics_reports_last_success(self):
        latest = self.metrics.latest_metrics(self.device)
        self.assertEqual(latest["status"], "failed")  # m = 0 is a failed attempt
        self.assertIsNotNone(latest["last_success_at"])
        self.assertLess(latest["last_success_at"], latest["collected_at"])


if __name__ == "__main__":
    unittest.main()
