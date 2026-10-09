"""
Health classification semantics: ICMP / TCP-22 / SSH / CLI evidence -> healthy / degraded /
down with stable reason codes; separate network-unreachable streak; recovery; change grace;
the ICMP probe itself; persistence with and without migration 010.

    python -m unittest tests.test_health_classification -v
PostgreSQL tests need HEALTH_TEST_PG_DSN (a disposable database with `devices`).
"""

import os
import socket
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException  # noqa: E402

from coordination import device_ops as coord  # noqa: E402
from fakes import fake_redis  # noqa: E402
from health import checks, icmp  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
DEVICE = {"id": 3, "hostname": "Kenda-HQ-SW-01", "management_ip": "10.0.0.3", "platform": "dell_os6", "credential_path": "x"}
DOWN_AFTER = checks.DOWN_AFTER_FAILURES


def probe(icmp_ok=True, tcp=True, ssh=True, cli=True, failure=None, error=None, ms=800):
    return {"icmp_reachable": icmp_ok, "tcp_reachable": tcp, "ssh_reachable": ssh, "cli_reachable": cli,
            "response_time_ms": ms, "error": error, "failure": failure}


def run(sequence, previous=None):
    """Classify a sequence of probes, feeding each result back as `previous`."""
    rows = []
    for index, p in enumerate(sequence):
        previous = checks.classify(p, previous, NOW + timedelta(minutes=index))
        rows.append(previous)
    return rows


class MatrixTests(unittest.TestCase):
    """The requested test matrix (1-10) plus edge cases."""

    def test_01_all_ok_is_healthy(self):
        row = run([probe()])[0]
        self.assertEqual((row["status"], row["health_reason"], row["consecutive_failures"]), ("healthy", "ok", 0))

    def test_02_ping_ok_ssh_port_closed_is_degraded_not_down(self):
        rows = run([probe(tcp=False, ssh=False, cli=False, failure="tcp22_unreachable", error="TCP 22 unreachable: timeout")] * 5)
        self.assertEqual({r["status"] for r in rows}, {"degraded"})       # never down, however long it lasts
        self.assertEqual(rows[-1]["health_reason"], "ssh_management_unavailable")
        self.assertEqual(rows[-1]["unreachable_count"], 0)                 # does not accumulate towards down
        self.assertEqual(rows[-1]["consecutive_failures"], 5)              # still counts as a failed check
        self.assertIn("Reachable by ICMP, SSH management unavailable", rows[-1]["last_error"])

    def test_03_ssh_auth_failure_is_degraded(self):
        row = run([probe(ssh=False, cli=False, failure="ssh_authentication_failed", error="SSH authentication failed")])[0]
        self.assertEqual((row["status"], row["health_reason"]), ("degraded", "ssh_authentication_failed"))

    def test_04_cli_failure_is_degraded(self):
        row = run([probe(cli=False, failure="cli_verification_failed")])[0]
        self.assertEqual((row["status"], row["health_reason"]), ("degraded", "cli_verification_failed"))

    def test_05_icmp_blocked_but_management_ok_is_healthy(self):
        row = run([probe(icmp_ok=False)])[0]
        self.assertEqual((row["status"], row["health_reason"]), ("healthy", "ok"))
        self.assertFalse(row["icmp_reachable"])

    def test_06_icmp_blocked_and_ssh_failing_is_degraded(self):
        rows = run([probe(icmp_ok=False, ssh=False, cli=False, failure="ssh_timeout")] * 4)
        self.assertEqual({r["status"] for r in rows}, {"degraded"})  # TCP/22 answered: never down
        self.assertEqual(rows[-1]["health_reason"], "ssh_timeout")

    def test_07_first_full_failure_is_not_down(self):
        row = run([probe(icmp_ok=False, tcp=False, ssh=False, cli=False, error="TCP 22 unreachable: timeout")])[0]
        self.assertEqual((row["status"], row["health_reason"], row["unreachable_count"]), ("degraded", "network_unreachable", 1))
        self.assertIn(f"1 of {DOWN_AFTER} checks before down", row["last_error"])

    def test_08_consecutive_full_failures_reach_down(self):
        rows = run([probe(icmp_ok=False, tcp=False, ssh=False, cli=False, error="timeout")] * DOWN_AFTER)
        self.assertEqual(rows[-1]["status"], "down")
        self.assertEqual((rows[-1]["health_reason"], rows[-1]["unreachable_count"]), ("network_unreachable", DOWN_AFTER))

    def test_09_down_device_regaining_ping_but_not_ssh_is_degraded(self):
        rows = run([probe(icmp_ok=False, tcp=False, ssh=False, cli=False)] * DOWN_AFTER
                   + [probe(tcp=False, ssh=False, cli=False)])
        self.assertEqual(rows[-2]["status"], "down")
        self.assertEqual((rows[-1]["status"], rows[-1]["health_reason"], rows[-1]["unreachable_count"]),
                         ("degraded", "ssh_management_unavailable", 0))

    def test_10_degraded_device_regaining_ssh_and_cli_is_healthy(self):
        rows = run([probe(tcp=False, ssh=False, cli=False), probe()])
        self.assertEqual((rows[-1]["status"], rows[-1]["health_reason"], rows[-1]["consecutive_failures"]), ("healthy", "ok", 0))
        self.assertEqual(rows[-1]["last_success_at"], NOW + timedelta(minutes=1))

    # ---- further transitions / edge cases -----------------------------------------------------
    def test_healthy_to_ssh_unavailable_with_ping_is_degraded(self):
        rows = run([probe(), probe(tcp=False, ssh=False, cli=False)])
        self.assertEqual([r["status"] for r in rows], ["healthy", "degraded"])
        self.assertEqual(rows[1]["last_status_change_at"], NOW + timedelta(minutes=1))

    def test_degraded_then_persistent_full_outage_becomes_down(self):
        rows = run([probe(tcp=False, ssh=False, cli=False)] * 3 + [probe(icmp_ok=False, tcp=False, ssh=False, cli=False)] * DOWN_AFTER)
        self.assertEqual([r["status"] for r in rows], ["degraded"] * 3 + ["degraded"] * (DOWN_AFTER - 1) + ["down"])

    def test_management_failures_do_not_count_towards_down(self):
        # Many auth failures, then ONE network blip: not down (old logic went down here).
        rows = run([probe(ssh=False, cli=False, failure="ssh_authentication_failed")] * 6
                   + [probe(icmp_ok=False, tcp=False, ssh=False, cli=False)])
        self.assertEqual(rows[-1]["consecutive_failures"], 7)
        self.assertEqual((rows[-1]["status"], rows[-1]["unreachable_count"]), ("degraded", 1))

    def test_icmp_not_testable_falls_back_to_tcp22_rule(self):
        rows = run([probe(icmp_ok=None, tcp=False, ssh=False, cli=False)] * DOWN_AFTER)
        self.assertEqual(rows[0]["status"], "degraded")
        self.assertEqual((rows[-1]["status"], rows[-1]["health_reason"]), ("down", "tcp22_unreachable"))
        self.assertIn("ICMP not available", rows[-1]["last_error"])
        self.assertIsNone(rows[-1]["icmp_reachable"])

    def test_slow_response_stays_degraded_with_reason(self):
        row = run([probe(ms=checks.SLOW_THRESHOLD_MS + 1)])[0]
        self.assertEqual((row["status"], row["health_reason"]), ("degraded", "slow_response"))

    def test_streak_fallback_before_migration_010(self):
        # A row written by the old worker (no unreachable_count): the old counter is used
        # only if TCP/22 was already failing, so behaviour stays continuous across the upgrade.
        old_down_candidate = {"status": "degraded", "consecutive_failures": 1, "last_success_at": None,
                              "last_status_change_at": None, "tcp_reachable": False}
        self.assertEqual(checks.classify(probe(icmp_ok=False, tcp=False, ssh=False, cli=False), old_down_candidate, NOW)["status"], "down")
        old_auth_failures = {**old_down_candidate, "consecutive_failures": 9, "tcp_reachable": True}
        self.assertEqual(checks.classify(probe(icmp_ok=False, tcp=False, ssh=False, cli=False), old_auth_failures, NOW)["status"], "degraded")


class ProbeFailureCodeTests(unittest.TestCase):
    """probe_device maps each failing stage to a reason code (no real network)."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.icmp = mock.patch.object(checks, "icmp_echo", return_value=True).start()
        self.tcp = mock.patch.object(checks, "_tcp_reachable", return_value=(True, None)).start()
        mock.patch.object(checks, "get_device_credentials", return_value={"username": "u", "password": "pw", "secret": ""}).start()
        self.session = mock.Mock()
        self.session.send_command.return_value = "Dell Networking N3248, software 6.8.1.0 ...................."
        self.open = mock.patch.object(checks, "_open_session", return_value=self.session).start()

    def test_codes(self):
        self.assertIsNone(checks.probe_device(DEVICE)["failure"])
        self.open.reset_mock()
        self.tcp.return_value = (False, "TCP 22 unreachable: timeout")
        p = checks.probe_device(DEVICE)
        self.assertEqual((p["failure"], p["icmp_reachable"], p["ssh_reachable"]), ("tcp22_unreachable", True, False))
        self.open.assert_not_called()
        self.tcp.return_value = (True, None)
        for exc, code in ((NetmikoAuthenticationException("bad"), "ssh_authentication_failed"),
                          (NetmikoTimeoutException("slow"), "ssh_timeout"),
                          (socket.timeout("t"), "ssh_timeout"),
                          (EOFError("closed"), "ssh_session_failed")):
            self.open.side_effect = exc
            self.assertEqual(checks.probe_device(DEVICE)["failure"], code, code)
        self.open.side_effect = None
        self.session.send_command.side_effect = TimeoutError("read")
        self.assertEqual(checks.probe_device(DEVICE)["failure"], "cli_verification_failed")
        self.session.send_command.side_effect = None
        self.session.send_command.return_value = "% Invalid input detected at '^' marker."
        p = checks.probe_device(DEVICE)
        self.assertEqual((p["failure"], p["ssh_reachable"], p["cli_reachable"]), ("cli_verification_failed", True, False))
        self.assertEqual(checks.probe_device({**DEVICE, "platform": "junos"})["failure"], "unsupported_platform")

    def test_icmp_is_probed_once_and_not_counted_in_response_time(self):
        self.icmp.side_effect = lambda host: time.sleep(0.2) or False
        p = checks.probe_device(DEVICE)
        self.icmp.assert_called_once_with("10.0.0.3")
        self.assertLess(p["response_time_ms"], 150)


class CheckAndRecordTests(unittest.TestCase):
    """End to end through check_and_record: coordination, grace, logging (in-memory Redis)."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.redis = fake_redis.install(self)
        self.rows = {}

        def record(device_id, fn):
            previous = self.rows.get(device_id)
            current = {**fn(previous), "device_id": device_id}
            self.rows[device_id] = current
            return previous, current

        mock.patch.object(checks, "record_health", side_effect=record).start()
        mock.patch.object(checks, "cache_device_health").start()
        self.probe = mock.patch.object(checks, "probe_device", return_value=probe()).start()

    def test_kenda_hq_sw_01_scenario(self):
        self.probe.return_value = probe(tcp=False, ssh=False, cli=False, failure="tcp22_unreachable", error="TCP 22 unreachable: timeout")
        with self.assertLogs("network_worker.health", "INFO") as logs:
            for _ in range(DOWN_AFTER + 2):
                row = checks.check_and_record(DEVICE)
        self.assertEqual((row["status"], row["health_reason"]), ("degraded", "ssh_management_unavailable"))
        line = [l for l in logs.output if "device_health_check_completed" in l][-1]
        for part in ("status=degraded", "reason=ssh_management_unavailable", "icmp_reachable=True",
                     "tcp22_reachable=False", "ssh_authenticated=False", "cli_verified=False"):
            self.assertIn(part, line)
        self.assertNotIn(" tcp=", line)

    def test_change_grace_still_holds_status_first(self):
        self.rows[3] = run([probe()])[0]
        lease = coord.acquire_device_operation(3, "apply").lease
        coord.set_change_in_progress(lease, "chg-1", "job-1", 600)
        self.probe.return_value = probe(tcp=False, ssh=False, cli=False)  # e.g. SSH restarting after the change
        row = checks.check_and_record(DEVICE, lease=lease)
        self.assertEqual(row["status"], "healthy")  # grace semantics first (no false degradation)
        self.assertIn("transition held for grace", row["last_error"])
        with mock.patch.object(checks.time, "time", return_value=time.time() + coord.CHANGE_HEALTH_GRACE_SECONDS + 1):
            row = checks.check_and_record(DEVICE, lease=lease)
        self.assertEqual((row["status"], row["health_reason"]), ("degraded", "ssh_management_unavailable"))  # then normal rules
        coord.clear_change_in_progress(lease)
        lease.release()

    def test_tcp_only_observation_during_change_keeps_reason(self):
        self.rows[3] = run([probe(cli=False, failure="cli_verification_failed")])[0]
        lease = coord.acquire_device_operation(3, "apply").lease
        coord.set_change_in_progress(lease, "chg-2", "job-2", 600)
        self.probe.side_effect = lambda device, tcp_only=False: {**probe(), "tcp_only": tcp_only}
        row = checks.check_and_record(DEVICE)
        self.assertEqual(self.probe.call_args.kwargs, {"tcp_only": True})  # coordination unchanged: no SSH
        self.assertEqual((row["status"], row["health_reason"]), ("degraded", "cli_verification_failed"))
        coord.clear_change_in_progress(lease)
        lease.release()

    def test_health_lock_is_still_taken(self):
        checks.check_and_record(DEVICE)
        self.assertIsNone(coord.current_operation(3))  # acquired and released
        telemetry = coord.acquire_device_operation(3, "telemetry")
        self.assertEqual(checks.check_and_record(DEVICE)["coordination_mode"], "bypass:telemetry")
        telemetry.lease.release()


class IcmpProbeTests(unittest.TestCase):
    def test_reply_timeout_and_unavailable(self):
        if not icmp.icmp_available():
            self.skipTest("unprivileged ICMP not permitted here (net.ipv4.ping_group_range)")
        self.assertTrue(icmp.icmp_echo("127.0.0.1"))
        started = time.monotonic()
        self.assertFalse(icmp.icmp_echo("192.0.2.1", timeout=0.5))  # TEST-NET-1: never answers
        self.assertLess(time.monotonic() - started, 1.5)            # bounded

    def test_not_permitted_returns_none_never_false(self):
        with mock.patch.object(icmp.socket, "socket", side_effect=PermissionError(1, "Operation not permitted")):
            self.assertIsNone(icmp.icmp_echo("127.0.0.1"))

    def test_disabled_by_env(self):
        with mock.patch.object(icmp, "ICMP_ENABLED", False):
            self.assertIsNone(icmp.icmp_echo("127.0.0.1"))

    def test_checksum(self):
        packet = icmp._packet(7)
        self.assertEqual(icmp._checksum(packet), 0)  # a valid checksum sums to zero


DSN = os.getenv("HEALTH_TEST_PG_DSN")


@unittest.skipUnless(DSN, "HEALTH_TEST_PG_DSN not set (disposable PostgreSQL)")
class PersistenceTests(unittest.TestCase):
    """record_health writes the evidence columns after migration 010 and keeps working without it."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        from urllib.parse import urlparse

        from db import client, health as db_health

        url = urlparse(DSN)
        client.DB_HOST, client.DB_PORT, client.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
        client.DB_USER, client.DB_PASSWORD = url.username, url.password
        cls.db, cls.conn = db_health, psycopg.connect(DSN, autocommit=True)
        migrations = os.path.join(HERE, "..", "..", "..", "db", "migrations")
        cls.sql = {n: open(os.path.join(migrations, n)).read() for n in
                   ("001_device_health.sql", "010_health_evidence.sql", "010_health_evidence.down.sql")}
        cls.conn.execute(cls.sql["001_device_health.sql"])
        cls.device = cls.conn.execute("SELECT id FROM devices ORDER BY id LIMIT 1").fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.execute("DELETE FROM device_health WHERE device_id = %s", (self.device,))
        self.db._evidence.update(available=None, checked_at=0.0)

    def record(self, p):
        return self.db.record_health(self.device, lambda prev: checks.classify(p, prev, datetime.now(timezone.utc)))

    def test_without_migration_010_health_still_records(self):
        self.conn.execute(self.sql["010_health_evidence.down.sql"])
        _, row = self.record(probe(tcp=False, ssh=False, cli=False))
        self.assertEqual(row["status"], "degraded")
        self.assertNotIn("health_reason", row)

    def test_with_migration_010_evidence_and_streak_persist(self):
        self.conn.execute(self.sql["010_health_evidence.sql"])
        self.conn.execute(self.sql["010_health_evidence.sql"])  # re-run safe
        _, row = self.record(probe(tcp=False, ssh=False, cli=False))
        self.assertEqual((row["status"], row["health_reason"], row["icmp_reachable"]), ("degraded", "ssh_management_unavailable", True))
        for _ in range(DOWN_AFTER):
            _, row = self.record(probe(icmp_ok=False, tcp=False, ssh=False, cli=False))
        self.assertEqual((row["status"], row["unreachable_count"], row["health_reason"]), ("down", DOWN_AFTER, "network_unreachable"))
        _, row = self.record(probe(tcp=False, ssh=False, cli=False))  # ping returns
        self.assertEqual((row["status"], row["unreachable_count"]), ("degraded", 0))
        _, row = self.record(probe())
        self.assertEqual((row["status"], row["health_reason"], row["consecutive_failures"]), ("healthy", "ok", 0))


if __name__ == "__main__":
    unittest.main()
