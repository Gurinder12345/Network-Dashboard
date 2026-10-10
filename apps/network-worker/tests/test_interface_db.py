"""
Migration 009 + db/interfaces.py against a REAL, disposable PostgreSQL. Skipped unless
IFACE_TEST_PG_DSN points at a throwaway database with `devices` (and topology_links from
migration 003). Never point this at the production database.

    IFACE_TEST_PG_DSN=postgresql://user:pw@127.0.0.1:55443/ifmon python -m unittest tests.test_interface_db -v

Also prints measured bulk-write timings for the current lab size (10 devices x 50
interfaces) and a 100-device synthetic fleet (no switches involved).
"""

import os
import statistics
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import urlparse

DSN = os.getenv("IFACE_TEST_PG_DSN")
HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS = os.path.join(HERE, "..", "..", "..", "db", "migrations")
sys.path.insert(0, os.path.dirname(HERE))


def item(n, **kw):
    base = {"interface_name": f"Te1/0/{n}", "canonical_name": f"te1/0/{n}", "description": f"PORT-{n}",
            "interface_type": "ethernet", "admin_status": "up", "oper_status": "up", "speed_bps": 10**10,
            "duplex": "full", "mode": "trunk" if n % 4 == 0 else "access", "access_vlan": None if n % 4 == 0 else 10,
            "native_vlan": 1 if n % 4 == 0 else None, "allowed_vlans": "10-20" if n % 4 == 0 else None,
            "port_channel": None, "mtu": 9216, "role": None, "last_state_change": None,
            "rx_bytes": 10**9 + n, "tx_bytes": 2 * 10**9 + n, "rx_packets": 10**6, "tx_packets": 10**6,
            "rx_errors": 0, "tx_errors": 0, "crc_errors": 0, "input_discards": 0, "output_discards": 0}
    base.update(kw)
    return base


@unittest.skipUnless(DSN, "IFACE_TEST_PG_DSN not set (needs a disposable PostgreSQL)")
class InterfaceDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        url = urlparse(DSN)
        from db import client, interfaces
        from interfaces import utilization

        client.DB_HOST, client.DB_PORT, client.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
        client.DB_USER, client.DB_PASSWORD = url.username, url.password
        cls.psycopg, cls.db, cls.util = psycopg, interfaces, utilization
        with open(os.path.join(MIGRATIONS, "009_interface_monitoring.sql")) as h:
            cls.up = h.read()
        with open(os.path.join(MIGRATIONS, "009_interface_monitoring.down.sql")) as h:
            cls.down = h.read()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(cls.down)
            conn.execute(cls.up)
            conn.execute(cls.up)  # re-run must be safe
            conn.execute(cls.down)  # rollback works
            conn.execute(cls.up)
            have = conn.execute("SELECT count(*) FROM devices").fetchone()[0]
            for n in range(have, 100):
                conn.execute("INSERT INTO devices (hostname, management_ip, platform, enabled) VALUES (%s, %s, 'dell_os6', true)",
                             (f"sim-sw-{n:03d}", f"198.51.100.{n % 250 + 1}"))
            cls.devices = [r[0] for r in conn.execute("SELECT id FROM devices ORDER BY id LIMIT 100")]

    def setUp(self):
        self.conn = self.psycopg.connect(DSN, autocommit=True)
        self.conn.execute("TRUNCATE interface_metrics, interface_inventory RESTART IDENTITY")

    def tearDown(self):
        self.conn.close()

    def persist(self, device, at, items):
        return self.db.persist_collection(device, at, items, 900,
                                          lambda prev, it: self.util.compute(prev, it, at, 900))

    def test_bulk_write_is_one_transaction_with_fixed_statements(self):
        from db import client

        connections = []
        real = client.get_connection
        with mock.patch.object(self.db, "get_connection", side_effect=lambda: connections.append(1) or real()):
            rows, statements = self.persist(self.devices[0], datetime.now(timezone.utc), [item(n) for n in range(1, 51)])
        self.assertEqual(len(connections), 1)       # one connection / transaction
        self.assertEqual(statements, 3)             # upsert, previous, insert -- not 50 x N
        self.assertEqual(len(rows), 50)
        counts = self.conn.execute("SELECT (SELECT count(*) FROM interface_inventory), (SELECT count(*) FROM interface_metrics)").fetchone()
        self.assertEqual(counts, (50, 50))
        self.assertTrue(all(r["rx_utilization_pct"] is None for r in rows))  # first sample

    def test_second_sample_reset_and_upsert(self):
        device, t1 = self.devices[0], datetime.now(timezone.utc) - timedelta(seconds=300)
        self.persist(device, t1, [item(1), item(2), item(3)])
        t2 = t1 + timedelta(seconds=300)
        rows, _ = self.persist(device, t2, [item(1, rx_bytes=10**9 + 1 + 37_500_000_000), item(2, rx_bytes=5),
                                            item(3, description="RENAMED", crc_errors=4)])
        by = {r["canonical_name"]: r for r in rows}
        self.assertEqual(by["te1/0/1"]["rx_utilization_pct"], 10.0)
        self.assertIsNone(by["te1/0/2"]["rx_utilization_pct"])  # counter reset -> NULL, never negative
        self.assertTrue(by["te1/0/3"]["erroring"])
        stored = self.conn.execute("SELECT description FROM interface_inventory WHERE canonical_name='te1/0/3'").fetchone()[0]
        self.assertEqual(stored, "RENAMED")
        pct = self.conn.execute("SELECT rx_utilization_pct FROM interface_metrics WHERE collected_at = %s ORDER BY interface_id", (t2,)).fetchall()
        self.assertEqual([float(p[0]) if p[0] is not None else None for p in pct], [10.0, None, 0.0])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM interface_inventory").fetchone()[0], 3)  # upsert, no duplicates

    def test_last_state_change_observed_and_reported(self):
        device, t1 = self.devices[0], datetime.now(timezone.utc) - timedelta(seconds=600)
        self.persist(device, t1, [item(1), item(2)])
        t2 = t1 + timedelta(seconds=300)
        reported = t2 - timedelta(hours=5)
        rows, _ = self.persist(device, t2, [item(1, oper_status="down"), item(2, last_state_change=reported)])
        by = {r["canonical_name"]: r for r in rows}
        self.assertEqual(by["te1/0/1"]["last_state_change"], t2)        # flip observed at this poll
        self.assertEqual(by["te1/0/2"]["last_state_change"], reported)  # device-reported value wins
        rows, _ = self.persist(device, t2 + timedelta(seconds=300), [item(1, oper_status="down"), item(2)])
        self.assertEqual({r["canonical_name"]: r for r in rows}["te1/0/1"]["last_state_change"], t2)  # unchanged

    def test_missing_interface_is_kept_not_deleted(self):
        device, t1 = self.devices[0], datetime.now(timezone.utc) - timedelta(seconds=300)
        self.persist(device, t1, [item(1), item(2)])
        self.persist(device, t1 + timedelta(seconds=300), [item(1)])
        rows = self.conn.execute("SELECT canonical_name, last_seen FROM interface_inventory ORDER BY 1").fetchall()
        self.assertEqual([r[0] for r in rows], ["te1/0/1", "te1/0/2"])
        self.assertLess(rows[1][1], rows[0][1])

    def test_constraints_reject_fabricated_values(self):
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self.persist(self.devices[0], datetime.now(timezone.utc), [item(1, speed_bps=0)])
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self.conn.execute("INSERT INTO interface_inventory (device_id, interface_name, canonical_name, interface_type,"
                              " first_seen, last_seen, oper_status) VALUES (%s,'x','x','ethernet',now(),now(),'flapping')",
                              (self.devices[0],))

    def test_index_usage_and_measured_timings(self):
        """Loads ~2 days of 5-minute history for 10 x 50 interfaces, then EXPLAINs the reads."""
        start = datetime.now(timezone.utc) - timedelta(days=2)
        items = [item(n) for n in range(1, 51)]
        lab = self.devices[:10]
        for device in lab:
            self.persist(device, start - timedelta(seconds=300), items)
        ids = [r[0] for r in self.conn.execute("SELECT id FROM interface_inventory ORDER BY id")]
        # Bulk history via generate_series (SQL-side, fast) -- 576 samples per interface.
        self.conn.execute("""
            INSERT INTO interface_metrics (device_id, interface_id, collected_at, rx_bytes, tx_bytes, rx_packets, tx_packets,
                                           rx_errors, tx_errors, crc_errors, input_discards, output_discards,
                                           rx_utilization_pct, tx_utilization_pct)
            SELECT i.device_id, i.id, %s + make_interval(secs => g * 300), 812345678901 + g * 1000, 712345678901 + g * 2000,
                   912345678 + g, 812345678 + g, 0, 0, g / 100, 2, 9, 1.5, 0.5
            FROM interface_inventory i, generate_series(0, 575) g
        """, (start,))
        self.conn.execute("ANALYZE interface_metrics")
        total = self.conn.execute("SELECT count(*) FROM interface_metrics").fetchone()[0]

        def plan(sql, args):
            return "\n".join(r[0] for r in self.conn.execute("EXPLAIN " + sql, args))

        history = plan("SELECT collected_at, rx_utilization_pct FROM interface_metrics WHERE interface_id = %s "
                       "AND collected_at >= now() - interval '24 hours' ORDER BY collected_at", (ids[0],))
        self.assertIn("interface_metrics_interface_time_idx", history)
        latest = plan("SELECT DISTINCT ON (m.interface_id) m.* FROM interface_metrics m JOIN interface_inventory i ON i.id = m.interface_id "
                      "WHERE i.device_id = %s AND m.device_id = %s AND m.collected_at >= now() - interval '1 hour' "
                      "ORDER BY m.interface_id, m.collected_at DESC", (lab[0], lab[0]))
        self.assertRegex(latest, r"interface_metrics_(device|interface)_time_idx")
        self.assertNotIn("Seq Scan on interface_metrics", latest + history)
        previous = plan(self.db._PREVIOUS, (lab[0], datetime.now(timezone.utc), 900, datetime.now(timezone.utc)))
        self.assertIn("interface_metrics_device_time_idx", previous)

        # Measured: one 10-device x 50-interface cycle, then 100 devices x 50.
        def cycle(devices, at):
            times = []
            for device in devices:
                t0 = time.perf_counter()
                self.persist(device, at, items)
                times.append((time.perf_counter() - t0) * 1000)
            return times

        lab_times = cycle(lab, datetime.now(timezone.utc))
        sim_times = cycle(self.devices[:100], datetime.now(timezone.utc) + timedelta(seconds=1))
        size = self.conn.execute("SELECT pg_total_relation_size('interface_metrics'), pg_relation_size('interface_metrics')").fetchone()
        rows_now = self.conn.execute("SELECT count(*) FROM interface_metrics").fetchone()[0]
        print(f"\n    history rows loaded: {total}")
        print(f"    persist per device (50 interfaces), 10-device cycle: median {statistics.median(lab_times):.1f} ms, "
              f"max {max(lab_times):.1f} ms, total {sum(lab_times):.0f} ms")
        print(f"    persist per device (50 interfaces), 100-device cycle: median {statistics.median(sim_times):.1f} ms, "
              f"max {max(sim_times):.1f} ms, total {sum(sim_times):.0f} ms")
        print(f"    interface_metrics: {rows_now} rows, table {size[1] / 1024 / 1024:.1f} MiB, "
              f"with indexes {size[0] / 1024 / 1024:.1f} MiB -> {size[0] / rows_now:.0f} bytes/row")


if __name__ == "__main__":
    unittest.main()
