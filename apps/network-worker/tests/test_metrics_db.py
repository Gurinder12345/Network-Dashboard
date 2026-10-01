"""
Migration 005 + db.metrics against a REAL, disposable PostgreSQL. Skipped unless
TOPOLOGY_TEST_PG_DSN points at a throwaway database with a `devices` table.
Never point this at the production database.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

DSN = os.getenv("TOPOLOGY_TEST_PG_DSN")
HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS = os.path.join(HERE, "..", "..", "..", "db", "migrations")


@unittest.skipUnless(DSN, "TOPOLOGY_TEST_PG_DSN not set (needs a disposable PostgreSQL)")
class MetricsMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        url = urlparse(DSN)
        sys.path.insert(0, os.path.dirname(HERE))
        from db import client, metrics

        # db.client reads its env at import (possibly already imported by another test).
        client.DB_HOST, client.DB_PORT, client.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
        client.DB_USER, client.DB_PASSWORD = url.username, url.password

        cls.psycopg, cls.metrics = psycopg, metrics
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS device_metrics")
            sql = open(os.path.join(MIGRATIONS, "005_device_metrics.sql")).read()
            conn.execute(sql)
            conn.execute(sql)  # re-run must be safe
            ids = [r[0] for r in conn.execute("SELECT id FROM devices ORDER BY id LIMIT 1")]
        if not ids:
            raise unittest.SkipTest("disposable DB needs a row in devices")
        cls.device = ids[0]

    def setUp(self):
        self.conn = self.psycopg.connect(DSN, autocommit=True)
        self.conn.execute("DELETE FROM device_metrics")

    def tearDown(self):
        self.conn.close()

    def sample(self, **overrides):
        return {"device_id": self.device, "collected_at": datetime.now(timezone.utc), "cpu_percent": 10.0,
                "memory_percent": 40.0, "memory_used_mb": 800.0, "memory_total_mb": 2000.0, "uptime_seconds": 100,
                "source": "cli:test", "status": "success", "error": None, **overrides}

    def test_insert_and_last_success_ordering(self):
        now = datetime.now(timezone.utc)
        self.metrics.insert_metric(self.sample(collected_at=now - timedelta(minutes=2)))
        self.metrics.insert_metric(self.sample(collected_at=now - timedelta(minutes=1), status="partial", memory_percent=None))
        self.metrics.insert_metric(self.sample(collected_at=now, status="failed", cpu_percent=None, memory_percent=None,
                                               memory_used_mb=None, memory_total_mb=None, uptime_seconds=None, error="Device unreachable"))
        self.assertEqual(self.metrics.last_success_at(self.device), now - timedelta(minutes=1))
        rows = self.conn.execute("SELECT collection_status FROM device_metrics WHERE device_id = %s ORDER BY collected_at DESC",
                                 (self.device,)).fetchall()
        self.assertEqual([r[0] for r in rows], ["failed", "partial", "success"])

    def test_constraints(self):
        bad = [
            {"cpu_percent": 100.5},
            {"memory_percent": -1},
            {"status": "unknown"},
            {"status": "success", "memory_percent": None},          # success needs both
            {"status": "failed"},                                   # failed must have no metrics
            {"status": "partial"},                                  # partial needs exactly one
            {"memory_total_mb": 0},
            {"uptime_seconds": -5},
            {"device_id": 999999},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises((self.psycopg.errors.CheckViolation, self.psycopg.errors.ForeignKeyViolation)):
                    self.metrics.insert_metric(self.sample(**overrides))

    def test_index_exists(self):
        rows = self.conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'device_metrics_device_time_idx'").fetchall()
        self.assertIn("(device_id, collected_at DESC)", rows[0][0])


if __name__ == "__main__":
    unittest.main()
