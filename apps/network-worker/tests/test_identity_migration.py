"""
Migration 004 constraint tests against a REAL PostgreSQL. Skipped unless
TOPOLOGY_TEST_PG_DSN points at a disposable database that already has a `devices` table,
e.g.  TOPOLOGY_TEST_PG_DSN=postgresql://networkapp:x@127.0.0.1:55435/networkdb
Never point this at the production database.
"""

import os
import unittest

DSN = os.getenv("TOPOLOGY_TEST_PG_DSN")
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "..", "db", "migrations", "004_device_lldp_identity.sql")


@unittest.skipUnless(DSN, "TOPOLOGY_TEST_PG_DSN not set (needs a disposable PostgreSQL)")
class IdentityMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.psycopg = psycopg
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS device_lldp_identity")
            conn.execute(open(MIGRATION).read())
            conn.execute(open(MIGRATION).read())  # re-run must be safe
            ids = [row[0] for row in conn.execute("SELECT id FROM devices ORDER BY id LIMIT 2")]
        if len(ids) < 2:
            raise unittest.SkipTest("disposable DB needs at least 2 rows in devices")
        # Use whatever devices exist; never assume specific IDs.
        cls.A, cls.B = ids

    def setUp(self):
        self.conn = self.psycopg.connect(DSN, autocommit=True)
        self.conn.execute("DELETE FROM device_lldp_identity")

    def tearDown(self):
        self.conn.close()

    def insert(self, device_id, name=None, chassis=None):
        self.conn.execute(
            "INSERT INTO device_lldp_identity (device_id, lldp_system_name, chassis_id) VALUES (%s, %s, %s)",
            (device_id, name, chassis),
        )

    def assertRejected(self, *args):
        with self.assertRaises(self.psycopg.errors.IntegrityError):
            self.insert(*args)

    def test_duplicate_system_name_rejected_case_insensitively(self):
        self.insert(self.A, "kenda-core-02")
        self.assertRejected(self.B, "KENDA-CORE-02")

    def test_duplicate_chassis_rejected(self):
        self.insert(self.A, None, "e8:b5:d0:7a:5c:a3")
        self.assertRejected(self.B, None, "e8:b5:d0:7a:5c:a3")

    def test_non_canonical_mac_rejected(self):
        for form in ("E8:B5:D0:7A:5C:A3", "e8-b5-d0-7a-5c-a3", "e8b5.d07a.5ca3", "e8b5d07a5ca3", " e8:b5:d0:7a:5c:a3"):
            self.assertRejected(self.A, None, form)

    def test_requires_name_or_chassis(self):
        self.assertRejected(self.A, None, None)

    def test_untrimmed_or_empty_name_rejected(self):
        self.assertRejected(self.A, " kenda-core-02")
        self.assertRejected(self.A, "")

    def test_multiple_identities_per_device_allowed(self):
        self.insert(self.A, "HARO_SW_01")
        self.insert(self.A, None, "f0:d4:e2:95:6b:1d")
        self.insert(self.A, "haro-sw-01-old")
        count = self.conn.execute("SELECT count(*) FROM device_lldp_identity WHERE device_id = %s", (self.A,)).fetchone()[0]
        self.assertEqual(count, 3)

    def test_unknown_device_rejected(self):
        with self.assertRaises(self.psycopg.errors.ForeignKeyViolation):
            self.insert(-1, "ghost")


if __name__ == "__main__":
    unittest.main()
