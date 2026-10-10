"""
Interface Monitoring read API.

Unit tests (always): Redis and PostgreSQL mocked -- cache hit/miss/expired/schema/Redis
failure, stale semantics, one-request snapshot, detail, history validation, response size.

Integration (opt-in, disposable services only -- never production):
    IFACE_TEST_PG_DSN=postgresql://...:55443/ifmon IFACE_TEST_REDIS_URL=redis://127.0.0.1:56379/1 \
        python -m unittest tests.test_interfaces_api -v
real SQL (one query per fallback, no N+1), real Redis hit path, and measured latencies.
IFACE_SCALE_TEST=1 adds a 100-device x 50-interface synthetic dataset.

Run from apps/network-api.
"""

import json
import os
import statistics
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import cache, interfaces  # noqa: E402
from app.main import app  # noqa: E402

DEVICE = {"id": 12, "hostname": "Kenda-Core-2", "management_ip": "10.0.0.20", "platform": "dell_os10", "enabled": True}
ALLOWED_INTERFACE_KEYS = {
    "id", "name", "canonical_name", "description", "type", "role", "admin_status", "oper_status", "status", "speed_bps",
    "duplex", "mode", "access_vlan", "native_vlan", "allowed_vlans", "port_channel", "mtu", "last_state_change",
    "rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "crc_errors", "input_discards",
    "output_discards", "rx_utilization_pct", "tx_utilization_pct", "errors_delta", "crc_delta", "discards_delta", "erroring",
}


def iso(dt):
    return dt.isoformat()


def interface(n, **kw):
    item = {"id": n, "name": f"Eth 1/1/{n}", "canonical_name": f"ethernet1/1/{n}", "description": f"PORT-{n}",
            "type": "ethernet", "role": None, "admin_status": "up", "oper_status": "up", "status": "up",
            "speed_bps": 10**10, "duplex": "full", "mode": "access", "access_vlan": 10, "native_vlan": None,
            "allowed_vlans": None, "port_channel": None, "mtu": 9216, "last_state_change": None,
            "rx_bytes": 123456789012, "tx_bytes": 98765432101, "rx_packets": 912345678, "tx_packets": 812345678,
            "rx_errors": 0, "tx_errors": 0, "crc_errors": 0, "input_discards": 0, "output_discards": 0,
            "rx_utilization_pct": 12.34, "tx_utilization_pct": 3.21, "errors_delta": 0, "crc_delta": 0,
            "discards_delta": 0, "erroring": False}
    item.update(kw)
    return item


def snapshot(collected_at=None, count=3, **kw):
    items = [interface(n) for n in range(1, count + 1)]
    snap = {"schema_version": 1, "device_id": 12, "hostname": "Kenda-Core-2", "platform": "dell_os10",
            "collected_at": iso(collected_at or datetime.now(timezone.utc)), "collection_status": "success", "problems": [],
            "poll_interval_seconds": 300, "summary": interfaces.summarize(items), "interfaces": items}
    snap.update(kw)
    return snap


class FakeCache:
    def __init__(self, values=None, fail=False):
        self.values = dict(values or {})
        self.fail = fail
        self.mget_calls = 0

    def mget(self, keys):
        self.mget_calls += 1
        if self.fail:
            raise redis.ConnectionError("down")
        return [self.values.get(k) for k in keys]


class ApiUnitTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.client = TestClient(app)
        self.redis = FakeCache()
        mock.patch.object(cache, "_redis", return_value=self.redis).start()
        self.set_json = mock.patch.object(cache, "set_json").start()
        self.device = mock.patch.object(interfaces, "get_device_by_id", side_effect=lambda i: DEVICE if i == 12 else None).start()
        self.db = mock.patch.object(interfaces, "latest_device_interfaces", return_value=[]).start()
        self.get_interface = mock.patch.object(interfaces, "get_interface", return_value=None).start()
        self.history = mock.patch.object(interfaces, "interface_history", return_value=[]).start()

    def cache_snapshot(self, snap, attempt=None):
        self.redis.values[interfaces.SNAPSHOT_KEY.format(device_id=12)] = json.dumps(snap)
        if attempt:
            self.redis.values[interfaces.ATTEMPT_KEY.format(device_id=12)] = json.dumps(attempt)

    def db_rows(self, seen):
        inv = {"id": 1, "interface_name": "Eth 1/1/1", "canonical_name": "ethernet1/1/1", "description": None,
               "interface_type": "ethernet", "role": "inter_switch", "admin_status": "up", "oper_status": "down",
               "speed_bps": 10**10, "duplex": "full", "mode": "trunk", "access_vlan": None, "native_vlan": 1,
               "allowed_vlans": "10-20", "port_channel": None, "mtu": 9216, "last_state_change": seen, "last_seen": seen}
        cur = {"collected_at": seen, "rx_bytes": 2000, "tx_bytes": 1000, "rx_packets": 20, "tx_packets": 10, "rx_errors": 5,
               "tx_errors": 0, "crc_errors": 3, "input_discards": 0, "output_discards": 7, "rx_utilization_pct": 1.5,
               "tx_utilization_pct": None}
        prev = {**cur, "collected_at": seen - timedelta(seconds=300), "rx_errors": 1, "crc_errors": 3, "output_discards": 2}
        return [(inv, cur, prev)]

    # ---- cache hit -----------------------------------------------------------------------------
    def test_cache_hit_one_request_draws_the_tab_without_postgresql(self):
        self.cache_snapshot(snapshot(count=48), attempt={"status": "success", "at": iso(datetime.now(timezone.utc))})
        body = self.client.get("/api/v1/devices/12/interfaces").json()
        self.assertEqual(body["source"], "cache")
        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["stale"])
        self.assertEqual(body["summary"]["total"], 48)
        self.assertEqual(len(body["interfaces"]), 48)
        self.assertEqual(body["last_attempt"]["status"], "success")
        self.assertEqual(self.redis.mget_calls, 1)  # snapshot + attempt in ONE round trip
        self.device.assert_not_called()             # no PostgreSQL on a hit
        self.db.assert_not_called()
        self.set_json.assert_not_called()
        for item in body["interfaces"]:
            self.assertLessEqual(set(item), ALLOWED_INTERFACE_KEYS)  # normalized fields only, no raw CLI

    def test_summary_endpoint(self):
        self.cache_snapshot(snapshot())
        body = self.client.get("/api/v1/devices/12/interfaces/summary").json()
        self.assertNotIn("interfaces", body)
        self.assertEqual(body["summary"]["total"], 3)

    # ---- miss / expired / schema / Redis failure -> PostgreSQL ------------------------------------
    def test_cache_miss_and_expired_fall_back_to_postgresql_with_short_read_through(self):
        seen = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.db.return_value = self.db_rows(seen)
        body = self.client.get("/api/v1/devices/12/interfaces").json()
        self.assertEqual(body["source"], "database")
        self.db.assert_called_once()
        item = body["interfaces"][0]
        self.assertEqual(item["status"], "down")
        self.assertEqual((item["errors_delta"], item["crc_delta"], item["discards_delta"]), (4, 0, 5))
        self.assertTrue(item["erroring"])
        self.assertEqual(body["summary"]["erroring"], 1)
        key, value, ttl = self.set_json.call_args.args
        self.assertEqual((key, ttl, self.set_json.call_args.kwargs), ("interface:latest:device:12", 60, {"only_if_absent": True}))

    def test_redis_read_failure_falls_back_and_ui_keeps_working(self):
        self.redis.fail = True
        self.db.return_value = self.db_rows(datetime.now(timezone.utc))
        response = self.client.get("/api/v1/devices/12/interfaces")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "database")
        self.set_json.assert_not_called()  # no write attempts against a failing Redis

    def test_unknown_schema_version_is_a_miss_and_not_overwritten(self):
        self.cache_snapshot(snapshot(schema_version=2))
        self.db.return_value = self.db_rows(datetime.now(timezone.utc))
        self.assertEqual(self.client.get("/api/v1/devices/12/interfaces").json()["source"], "database")
        self.set_json.assert_not_called()

    def test_never_collected(self):
        body = self.client.get("/api/v1/devices/12/interfaces").json()
        self.assertEqual((body["status"], body["interfaces"], body["stale"], body["summary"]), ("not_collected", [], False, None))

    def test_unknown_device_and_database_unavailable(self):
        self.assertEqual(self.client.get("/api/v1/devices/99/interfaces").status_code, 404)
        self.db.side_effect = Exception("relation interface_inventory does not exist")
        self.assertEqual(self.client.get("/api/v1/devices/12/interfaces").status_code, 503)

    # ---- staleness ----------------------------------------------------------------------------
    def test_stale_is_labelled_never_converted_to_down(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=14)
        self.cache_snapshot(snapshot(collected_at=old), attempt={"status": "failed", "error": "Device unreachable (TCP 22)"})
        body = self.client.get("/api/v1/devices/12/interfaces").json()
        self.assertTrue(body["stale"])
        self.assertEqual(body["status"], "stale")
        self.assertGreaterEqual(body["age_seconds"], 14 * 60)
        self.assertEqual({i["status"] for i in body["interfaces"]}, {"up"})  # unchanged, not "down"
        self.assertEqual(body["last_attempt"]["status"], "failed")

    def test_fresh_boundary(self):
        self.cache_snapshot(snapshot(collected_at=datetime.now(timezone.utc) - timedelta(seconds=interfaces.STALE_AFTER_SECONDS - 30)))
        self.assertFalse(self.client.get("/api/v1/devices/12/interfaces").json()["stale"])

    # ---- detail ---------------------------------------------------------------------------------
    def test_interface_detail_from_snapshot_then_inventory(self):
        self.cache_snapshot(snapshot())
        body = self.client.get("/api/v1/devices/12/interfaces/2").json()
        self.assertEqual(body["interface"]["name"], "Eth 1/1/2")
        self.get_interface.assert_not_called()
        self.get_interface.return_value = {"id": 9, "interface_name": "Eth 1/1/9", "canonical_name": "ethernet1/1/9",
                                           "admin_status": "up", "oper_status": "up", "last_seen": datetime.now(timezone.utc)}
        body = self.client.get("/api/v1/devices/12/interfaces/9").json()
        self.assertTrue(body["not_in_latest_collection"])
        self.get_interface.return_value = None
        self.assertEqual(self.client.get("/api/v1/devices/12/interfaces/77").status_code, 404)

    # ---- history --------------------------------------------------------------------------------
    def test_history_only_for_the_requested_interface_with_deltas(self):
        self.get_interface.return_value = {"id": 5}
        t = datetime.now(timezone.utc) - timedelta(minutes=20)
        rows = [{"collected_at": t + timedelta(minutes=5 * i), "rx_utilization_pct": 1.0 * i, "tx_utilization_pct": None,
                 "rx_errors": e, "tx_errors": 0, "crc_errors": c, "input_discards": 0, "output_discards": 0}
                for i, (e, c) in enumerate([(0, 0), (2, 1), (1, 1), (4, 1)])]
        self.history.return_value = rows
        body = self.client.get("/api/v1/devices/12/interfaces/5/metrics?range=1h").json()
        self.assertEqual(self.history.call_args.args[:2], (12, 5))
        self.assertEqual([s["errors_delta"] for s in body["samples"]], [None, 2, None, 3])  # reset -> None
        self.assertEqual([s["crc_delta"] for s in body["samples"]], [None, 1, 0, 0])
        self.assertEqual(body["range"], "1h")

    def test_history_range_limits(self):
        self.get_interface.return_value = {"id": 5}
        ok = self.client.get("/api/v1/devices/12/interfaces/5/metrics?from=2026-10-01T00:00:00Z&to=2026-10-07T00:00:00Z")
        self.assertEqual(ok.status_code, 200)
        self.assertIsNone(ok.json()["range"])
        too_long = self.client.get("/api/v1/devices/12/interfaces/5/metrics?from=2026-09-01T00:00:00Z&to=2026-10-07T00:00:00Z")
        self.assertEqual(too_long.status_code, 422)
        naive = self.client.get("/api/v1/devices/12/interfaces/5/metrics?from=2026-10-01T00:00:00&to=2026-10-02T00:00:00")
        self.assertEqual(naive.status_code, 422)
        backwards = self.client.get("/api/v1/devices/12/interfaces/5/metrics?from=2026-10-02T00:00:00Z&to=2026-10-01T00:00:00Z")
        self.assertEqual(backwards.status_code, 422)
        self.assertEqual(self.client.get("/api/v1/devices/12/interfaces/5/metrics?range=30d").status_code, 422)
        self.get_interface.return_value = None
        self.assertEqual(self.client.get("/api/v1/devices/12/interfaces/6/metrics").status_code, 404)

    # ---- size ---------------------------------------------------------------------------------
    def test_response_size_for_a_52_port_switch(self):
        self.cache_snapshot(snapshot(count=52))
        response = self.client.get("/api/v1/devices/12/interfaces")
        size = len(response.content)
        print(f"\n    /interfaces response, 52 interfaces: {size} bytes ({size / 52:.0f} bytes/interface)")
        self.assertLess(size, 50_000)

    def test_fleet_summaries_one_mget(self):
        self.cache_snapshot(snapshot())
        result = interfaces.fleet_summaries([12, 13])
        self.assertEqual(result[12]["total"], 3)
        self.assertIsNone(result[13])


# ---- integration: real PostgreSQL + Redis ---------------------------------------------------------
DSN = os.getenv("IFACE_TEST_PG_DSN")
REDIS_URL = os.getenv("IFACE_TEST_REDIS_URL")


@unittest.skipUnless(DSN and REDIS_URL, "IFACE_TEST_PG_DSN / IFACE_TEST_REDIS_URL not set (disposable services)")
class ApiIntegrationTests(unittest.TestCase):
    INTERFACES = 50

    @classmethod
    def setUpClass(cls):
        import psycopg

        from app.db import client

        url = urlparse(DSN)
        client.DB_HOST, client.DB_PORT, client.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
        client.DB_USER, client.DB_PASSWORD = url.username, url.password
        cls.redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        cls.patcher = mock.patch.object(cache, "_client", cls.redis)
        cls.patcher.start()
        cls.pg = psycopg.connect(DSN, autocommit=True)
        migrations = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "db", "migrations")
        with open(os.path.join(migrations, "009_interface_monitoring.sql")) as handle:
            cls.pg.execute(handle.read())
        cls.pg.execute("TRUNCATE interface_metrics, interface_inventory RESTART IDENTITY")
        devices = 100 if os.getenv("IFACE_SCALE_TEST") else 10
        have = cls.pg.execute("SELECT count(*) FROM devices").fetchone()[0]
        for n in range(have, devices):
            cls.pg.execute("INSERT INTO devices (hostname, management_ip, platform, enabled) VALUES (%s, %s, 'dell_os6', true)",
                           (f"sim-sw-{n:03d}", f"198.51.100.{n % 250 + 1}"))
        cls.devices = [r[0] for r in cls.pg.execute("SELECT id FROM devices ORDER BY id LIMIT %s", (devices,))]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        cls.seen = now
        cls.pg.execute("""
            INSERT INTO interface_inventory (device_id, interface_name, canonical_name, description, interface_type,
                admin_status, oper_status, speed_bps, duplex, mode, access_vlan, mtu, first_seen, last_seen)
            SELECT d, 'Te1/0/' || n, 'te1/0/' || n, 'PORT-' || n, 'ethernet', 'up',
                   CASE WHEN n %% 10 = 0 THEN 'down' ELSE 'up' END, 10000000000, 'full',
                   CASE WHEN n %% 4 = 0 THEN 'trunk' ELSE 'access' END, CASE WHEN n %% 4 = 0 THEN NULL ELSE 10 END,
                   9216, %s, %s
            FROM unnest(%s::bigint[]) d, generate_series(1, %s) n
        """, (now - timedelta(days=1), now, cls.devices, cls.INTERFACES))
        # One day of 5-minute history per interface (288 samples), newest at `now`.
        cls.pg.execute("""
            INSERT INTO interface_metrics (device_id, interface_id, collected_at, rx_bytes, tx_bytes, rx_packets, tx_packets,
                rx_errors, tx_errors, crc_errors, input_discards, output_discards, rx_utilization_pct, tx_utilization_pct)
            SELECT i.device_id, i.id, %s - make_interval(secs => g * 300), 10^12 - g * 10^6, 10^12 - g * 10^6, 10^9 - g,
                   10^9 - g, 0, 0, CASE WHEN i.id %% 7 = 0 THEN 1000 - g ELSE 5 END, 0, 0, 1.25, 0.75
            FROM interface_inventory i, generate_series(0, 287) g
        """, (now,))
        cls.pg.execute("ANALYZE interface_metrics")
        cls.pg.execute("ANALYZE interface_inventory")
        cls.rows = cls.pg.execute("SELECT count(*) FROM interface_metrics").fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        cls.pg.close()

    def setUp(self):
        self.client = TestClient(app)
        self.redis.flushdb()

    def timed(self, path, n=50):
        times = []
        for _ in range(n):
            t0 = time.perf_counter()
            response = self.client.get(path)
            times.append((time.perf_counter() - t0) * 1000)
            self.assertEqual(response.status_code, 200, response.text)
        return statistics.median(times), sorted(times)[int(n * 0.95) - 1], response

    def test_fallback_is_one_query_and_matches_then_cache_hit(self):
        from app.db import interfaces as dbi

        import psycopg

        executed = []

        class CountingCursor(psycopg.Cursor):
            def execute(self, query, *args, **kwargs):
                executed.append(str(query)[:40])
                return super().execute(query, *args, **kwargs)

        def counting():
            return psycopg.connect(DSN, cursor_factory=CountingCursor)

        device = self.devices[0]
        with mock.patch.object(dbi, "get_connection", side_effect=counting):
            response = self.client.get(f"/api/v1/devices/{device}/interfaces")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(executed), 1)  # one interface query for 50 interfaces (no N+1)
        self.assertEqual(body["source"], "database")
        self.assertEqual(body["summary"]["total"], self.INTERFACES)
        self.assertEqual(body["summary"]["down"], self.INTERFACES // 10)
        self.assertEqual(body["summary"]["trunks"], self.INTERFACES // 4)
        erroring = [i for i in body["interfaces"] if i["erroring"]]
        self.assertEqual(len(erroring), sum(1 for i in body["interfaces"] if i["id"] % 7 == 0))
        # Read-through filled the (absent) key with a short TTL; the next request is a hit.
        ttl = self.redis.ttl(interfaces.SNAPSHOT_KEY.format(device_id=device))
        self.assertTrue(0 < ttl <= 60)
        self.assertEqual(self.client.get(f"/api/v1/devices/{device}/interfaces").json()["source"], "cache")

    def test_measured_latencies(self):
        device = self.devices[0]
        # Fallback: delete the key before every request.
        times = []
        for _ in range(30):
            self.redis.flushdb()
            t0 = time.perf_counter()
            self.assertEqual(self.client.get(f"/api/v1/devices/{device}/interfaces").json()["source"], "database")
            times.append((time.perf_counter() - t0) * 1000)
        fallback = (statistics.median(times), sorted(times)[27])
        hit_median, hit_p95, response = self.timed(f"/api/v1/devices/{device}/interfaces", n=200)
        self.assertEqual(response.json()["source"], "cache")
        interface_id = response.json()["interfaces"][0]["id"]
        h24 = self.timed(f"/api/v1/devices/{device}/interfaces/{interface_id}/metrics?range=24h", n=30)
        samples = len(h24[2].json()["samples"])
        print(f"\n    dataset: {len(self.devices)} devices x {self.INTERFACES} interfaces, {self.rows} metric rows")
        print(f"    /interfaces Redis hit    : median {hit_median:.1f} ms, p95 {hit_p95:.1f} ms ({len(response.content)} bytes)")
        print(f"    /interfaces PG fallback  : median {fallback[0]:.1f} ms, p95 {fallback[1]:.1f} ms")
        print(f"    /metrics?range=24h       : median {h24[0]:.1f} ms, p95 {h24[1]:.1f} ms ({samples} samples)")
        self.assertLess(hit_median, 100)


if __name__ == "__main__":
    unittest.main()
