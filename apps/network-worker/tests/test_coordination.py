"""
Per-device operation coordination (coordination/device_ops.py) and its integration with
health, telemetry, topology, manual backup, precheck and apply.

Every test runs against an in-memory Redis (tests/fakes/fake_redis.py). The CoordinationReal*
classes repeat the lock tests against a REAL Redis (validating the Lua scripts) when
COORD_TEST_REDIS_URL is set, e.g. a disposable container:
    COORD_TEST_REDIS_URL=redis://127.0.0.1:56393/15 python -m unittest tests.test_coordination -v
No switch is contacted.
"""

import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import redis

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from coordination import device_ops as coord  # noqa: E402
from fakes import fake_redis  # noqa: E402

REAL_URL = os.getenv("COORD_TEST_REDIS_URL")
DEVICE = {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "192.0.2.10", "platform": "dell_os10", "credential_path": "x"}
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class Backend:
    """Mixin: FakeRedis by default; subclasses switch to a real Redis."""
    real = False

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        if self.real:
            client = redis.Redis.from_url(REAL_URL, decode_responses=True)
            client.flushdb()
            self.client = fake_redis.install(self, client)
        else:
            self.client = fake_redis.install(self)
        # Fast waits for tests (production waits are seconds).
        mock.patch.object(coord, "POLL_INTERVAL_SECONDS", 0.02).start()
        ops = {k: dict(v) for k, v in coord.OPERATIONS.items()}
        ops["health"]["wait"] = 1
        ops["apply"]["wait"] = 3
        ops["precheck"]["wait"] = 1
        ops["backup"]["wait"] = 1
        mock.patch.object(coord, "OPERATIONS", ops).start()

    def pass_time(self, seconds):
        if self.real:
            time.sleep(seconds)
        else:
            self.client.advance(seconds)

    def acquire(self, operation, device_id=12, **kw):
        return coord.acquire_device_operation(device_id, operation, **kw)


# ---- lock semantics ------------------------------------------------------------------------
class LockTests(Backend, unittest.TestCase):
    def test_acquire_release_and_metadata(self):
        acq = self.acquire("telemetry", owner="task-1")
        self.assertTrue(acq.acquired)
        held = coord.current_operation(12)
        self.assertEqual({k: held[k] for k in ("device_id", "operation", "priority", "exclusive", "owner")},
                         {"device_id": 12, "operation": "telemetry", "priority": 3, "exclusive": False, "owner": "task-1"})
        self.assertTrue(held["started_at"] < held["expires_at"])
        self.assertGreater(self.client.ttl("device:12:operation"), 100)  # every slot has a TTL
        acq.lease.release()
        self.assertIsNone(coord.current_operation(12))

    def test_release_never_deletes_another_owners_slot(self):
        first = self.acquire("telemetry", ttl_seconds=1)
        self.pass_time(1.2)  # first owner's TTL expired (e.g. a stalled task)
        second = self.acquire("topology")
        self.assertTrue(second.acquired)
        first.lease.release()  # stale owner releases late
        self.assertEqual(coord.current_operation(12)["operation"], "topology")
        self.assertFalse(first.lease.refresh(60))  # and cannot extend it either
        self.assertEqual(coord.current_operation(12)["operation"], "topology")
        second.lease.release()

    def test_refresh_is_owner_only(self):
        acq = self.acquire("apply", ttl_seconds=5)
        self.assertTrue(acq.lease.refresh(300))
        self.assertGreater(self.client.ttl("device:12:operation"), 200)
        acq.lease.release()

    def test_every_key_has_a_ttl(self):
        acq = self.acquire("apply")
        coord.set_change_in_progress(acq.lease, "chg-1", "job-1", 120)
        coord.grace_started_at(12)
        for key in ("device:12:operation", "device:12:change-in-progress", "device:12:health-grace"):
            self.assertGreater(self.client.ttl(key), 0, key)
        coord.clear_change_in_progress(acq.lease)
        acq.lease.release()


class ContentionTests(Backend, unittest.TestCase):
    def test_telemetry_holds_topology_skips(self):
        telemetry = self.acquire("telemetry")
        topology = self.acquire("topology")
        self.assertEqual((topology.status, topology.reason), ("busy", "busy:telemetry"))
        self.assertLess(topology.wait_ms, 200)  # low priority never waits
        telemetry.lease.release()

    def test_health_waits_for_telemetry_and_collectors_yield_meanwhile(self):
        telemetry = self.acquire("telemetry")
        result = {}
        waiter = threading.Thread(target=lambda: result.update(health=self.acquire("health")))
        waiter.start()
        time.sleep(0.2)
        self.assertEqual(self.acquire("topology").status, "yield")  # health is waiting: topology yields
        telemetry.lease.release()
        waiter.join()
        self.assertTrue(result["health"].acquired)
        self.assertGreater(result["health"].wait_ms, 100)
        result["health"].lease.release()

    def test_health_gives_up_after_its_short_wait(self):
        telemetry = self.acquire("telemetry")
        health = self.acquire("health")
        self.assertEqual((health.status, health.holder["operation"]), ("busy", "telemetry"))
        self.assertGreaterEqual(health.wait_ms, 900)
        telemetry.lease.release()

    def test_change_waits_then_owns_device_and_collectors_skip(self):
        telemetry = self.acquire("telemetry")
        result = {}
        change = threading.Thread(target=lambda: result.update(apply=self.acquire("apply", owner="change:a1")))
        change.start()
        time.sleep(0.2)
        health = self.acquire("health", wait_seconds=0)
        self.assertEqual(health.status, "yield")  # a waiting change outranks health
        self.assertTrue(health.change_active)
        telemetry.lease.release()
        change.join()
        apply = result["apply"]
        self.assertTrue(apply.acquired)
        coord.set_change_in_progress(apply.lease, "a1", "job-1", 300)
        for op in ("telemetry", "topology", "interface_poll"):
            busy = self.acquire(op)
            self.assertEqual((busy.status, busy.change_active, busy.reason), ("busy", True, "change_in_progress"))
        self.assertEqual(self.acquire("backup").reason, "change_in_progress")
        coord.clear_change_in_progress(apply.lease)
        apply.lease.release()
        self.assertTrue(self.acquire("telemetry").acquired)

    def test_lower_priority_never_delays_a_waiting_change(self):
        holder = self.acquire("precheck")
        result = {}
        change = threading.Thread(target=lambda: result.update(apply=self.acquire("apply")))
        change.start()
        time.sleep(0.1)
        # While the change waits, nothing lower-priority can take the slot even if it frees up.
        holder.lease.release()
        change.join()
        self.assertTrue(result["apply"].acquired)
        result["apply"].lease.release()

    def test_no_two_operations_overlap_on_one_device(self):
        active, overlaps, lock = [0], [0], threading.Lock()

        def worker(op):
            for _ in range(15):
                acq = self.acquire(op, wait_seconds=0.3)
                if not acq.acquired:
                    continue
                with acq.lease:
                    with lock:
                        active[0] += 1
                        overlaps[0] += active[0] > 1
                    time.sleep(0.005)
                    with lock:
                        active[0] -= 1

        threads = [threading.Thread(target=worker, args=(op,)) for op in ("health", "telemetry", "topology", "backup")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(overlaps[0], 0)


class DifferentDeviceTests(Backend, unittest.TestCase):
    def test_devices_are_coordinated_independently(self):
        intervals, barrier = {}, threading.Barrier(3)

        def run(device_id, op):
            barrier.wait()
            acq = self.acquire(op, device_id=device_id)
            started = time.monotonic()
            time.sleep(0.2)
            intervals[device_id] = (acq.status, started, time.monotonic())
            acq.lease.release()

        threads = [threading.Thread(target=run, args=a) for a in ((12, "telemetry"), (13, "telemetry"), (14, "topology"))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual({v[0] for v in intervals.values()}, {"acquired"})
        latest_start = max(v[1] for v in intervals.values())
        earliest_end = min(v[2] for v in intervals.values())
        self.assertLess(latest_start, earliest_end)  # all three ran at the same time


class CrashRecoveryTests(Backend, unittest.TestCase):
    def test_dead_owner_slot_and_change_state_expire(self):
        acq = self.acquire("apply", ttl_seconds=1)
        coord.set_change_in_progress(acq.lease, "chg-crash", "job-1", 1)
        self.assertEqual(self.acquire("telemetry").reason, "change_in_progress")
        # The worker dies: nobody releases anything.
        self.pass_time(1.3)
        self.assertIsNone(coord.change_in_progress(12))
        self.assertTrue(self.acquire("telemetry").acquired)  # no manual Redis cleanup needed

    def test_waiter_marker_expires(self):
        holder = self.acquire("telemetry")
        self.client.set("device:12:operation:waiting", '{"token": "gone", "priority": 1, "operation": "apply"}', ex=1)
        self.assertEqual(self.acquire("topology").status, "yield")
        self.pass_time(1.3)
        holder.lease.release()
        self.assertTrue(self.acquire("topology").acquired)


class UnavailableTests(Backend, unittest.TestCase):
    def test_redis_down_returns_unavailable(self):
        self.client.down = True if not self.real else None
        if self.real:
            mock.patch.object(coord, "_redis", return_value=redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)).start()
        acq = self.acquire("telemetry")
        self.assertEqual((acq.status, acq.reason), ("unavailable", "coordination_unavailable"))
        self.assertIsNone(coord.change_in_progress(12))  # health keeps normal semantics
        self.assertIsNone(coord.grace_started_at(12))


@unittest.skipUnless(REAL_URL, "COORD_TEST_REDIS_URL not set (disposable Redis)")
class CoordinationRealLockTests(LockTests):
    real = True


@unittest.skipUnless(REAL_URL, "COORD_TEST_REDIS_URL not set (disposable Redis)")
class CoordinationRealContentionTests(ContentionTests):
    real = True


@unittest.skipUnless(REAL_URL, "COORD_TEST_REDIS_URL not set (disposable Redis)")
class CoordinationRealDeviceTests(DifferentDeviceTests):
    real = True


@unittest.skipUnless(REAL_URL, "COORD_TEST_REDIS_URL not set (disposable Redis)")
class CoordinationRealCrashTests(CrashRecoveryTests):
    real = True


@unittest.skipUnless(REAL_URL, "COORD_TEST_REDIS_URL not set (disposable Redis)")
class CoordinationRealUnavailableTests(UnavailableTests):
    real = True


# ---- health: change grace and real outages --------------------------------------------------
def health_row(status="healthy", failures=0):
    return {"status": status, "last_check_at": NOW, "last_success_at": NOW, "response_time_ms": 900,
            "tcp_reachable": True, "ssh_reachable": True, "cli_reachable": True, "last_error": None,
            "consecutive_failures": failures, "last_status_change_at": NOW - timedelta(hours=1)}


def probe(tcp=True, ssh=True, cli=True, error=None, tcp_only=False):
    p = {"tcp_reachable": tcp, "ssh_reachable": ssh, "cli_reachable": cli, "response_time_ms": 500, "error": error}
    if tcp_only:
        p["tcp_only"] = True
    return p


class HealthGraceTests(Backend, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from health import checks

        self.checks = checks
        self.rows = {}

        def record(device_id, fn):
            previous = self.rows.get(device_id)
            current = {**fn(previous), "device_id": device_id}  # like db.health.record_health
            self.rows[device_id] = current
            return previous, current

        mock.patch.object(checks, "record_health", side_effect=record).start()
        mock.patch.object(checks, "cache_device_health").start()  # rows here keep datetimes
        self.probe = mock.patch.object(checks, "probe_device", return_value=probe()).start()
        self.rows[12] = health_row()

    def start_change(self):
        lease = self.acquire("apply").lease
        coord.set_change_in_progress(lease, "chg-7", "job-7", 600)
        return lease

    def check(self, **kw):
        return self.checks.check_and_record(DEVICE, **kw)

    def test_first_failure_during_change_holds_status_and_keeps_evidence(self):
        lease = self.start_change()
        self.probe.return_value = probe(ssh=False, cli=False, error="SSH authentication failed")
        row = self.check(lease=lease)
        self.assertEqual(row["status"], "healthy")  # no false degraded transition
        self.assertEqual(row["consecutive_failures"], 1)  # the failure is recorded
        self.assertFalse(row["ssh_reachable"])
        self.assertIn("SSH authentication failed (change chg-7 in progress: transition held for grace", row["last_error"])
        self.assertEqual(row["last_status_change_at"], NOW - timedelta(hours=1))
        self.assertTrue(self.client.get("device:12:health-grace"))

    def test_persistent_failure_beyond_grace_degrades(self):
        lease = self.start_change()
        self.probe.return_value = probe(ssh=False, cli=False, error="SSH authentication failed")
        self.check(lease=lease)
        with mock.patch.object(coord.time, "time", return_value=time.time() + coord.CHANGE_HEALTH_GRACE_SECONDS + 1):
            row = self.check(lease=lease)
        self.assertEqual(row["status"], "degraded")
        self.assertEqual(row["last_error"], "SSH authentication failed")

    def test_real_outage_during_change_becomes_down_after_grace(self):
        lease = self.start_change()
        self.probe.return_value = probe(tcp=False, ssh=False, cli=False, error="TCP 22 unreachable: timeout")
        self.assertEqual(self.check(lease=lease)["status"], "healthy")  # held inside grace
        later = time.time() + coord.CHANGE_HEALTH_GRACE_SECONDS + 5
        with mock.patch.object(self.checks.time, "time", return_value=later):
            row = self.check(lease=lease)
        self.assertEqual((row["status"], row["consecutive_failures"]), ("down", 2))  # outage not suppressed
        with mock.patch.object(self.checks.time, "time", return_value=later + 60):
            self.assertEqual(self.check(lease=lease)["status"], "down")

    def test_change_completed_restores_normal_rules(self):
        lease = self.start_change()
        self.probe.return_value = probe(ssh=False, cli=False, error="SSH authentication failed")
        self.check(lease=lease)
        coord.clear_change_in_progress(lease)
        lease.release()
        self.assertIsNone(self.client.get("device:12:health-grace"))  # grace removed with the change
        self.assertEqual(self.check()["status"], "degraded")  # immediate normal semantics

    def test_success_during_change_is_normal_and_ends_grace(self):
        lease = self.start_change()
        self.probe.return_value = probe(ssh=False, cli=False, error="x")
        self.check(lease=lease)
        self.probe.return_value = probe()
        row = self.check(lease=lease)
        self.assertEqual((row["status"], row["consecutive_failures"]), ("healthy", 0))
        self.assertIsNone(self.client.get("device:12:health-grace"))

    def test_scheduled_health_during_apply_is_tcp_only_and_keeps_status(self):
        lease = self.start_change()  # another task (the change) owns the device
        self.probe.side_effect = lambda device, tcp_only=False: probe(tcp_only=tcp_only)
        row = self.check()
        self.assertEqual(self.probe.call_args.kwargs, {"tcp_only": True})  # no SSH during the change
        self.assertEqual((row["status"], row["coordination_mode"]), ("healthy", "tcp_only:apply"))
        self.assertEqual(row["last_success_at"], NOW)  # TCP-only never fakes a fresh success
        coord.clear_change_in_progress(lease)
        lease.release()

    def test_tcp_only_never_invents_health_for_unchecked_device(self):
        del self.rows[12]
        lease = self.start_change()
        self.probe.side_effect = lambda device, tcp_only=False: probe(tcp_only=tcp_only)
        self.assertEqual(self.check()["status"], "unknown")
        lease.release()

    def test_health_bypasses_a_slow_low_priority_collector(self):
        telemetry = self.acquire("telemetry")
        row = self.check()
        self.assertEqual(row["coordination_mode"], "bypass:telemetry")  # health never disappears
        self.assertEqual(self.probe.call_args.kwargs, {})
        telemetry.lease.release()

    def test_health_runs_unlocked_when_redis_is_down(self):
        self.client.down = True if not self.real else None
        row = self.check()
        self.assertEqual((row["status"], row["coordination_mode"]), ("healthy", "unlocked_redis_unavailable"))


# ---- collectors: skips never touch health or record failures ----------------------------------
class CollectorSkipTests(Backend, unittest.TestCase):
    def test_telemetry_skip_busy_and_change(self):
        from telemetry import collector

        insert = mock.patch.object(collector, "insert_metric").start()
        cache_metrics = mock.patch.object(collector, "cache_device_metrics").start()
        collect = mock.patch.object(collector, "collect_device").start()
        record_health = mock.patch("db.health.record_health").start()

        holder = self.acquire("topology")
        self.assertEqual(collector.collect_and_record(DEVICE)["status"], "skipped_busy")
        holder.lease.release()
        change = self.acquire("apply")
        coord.set_change_in_progress(change.lease, "chg-1", "job", 300)
        self.assertEqual(collector.collect_and_record(DEVICE)["status"], "skipped_change")
        for m in (insert, cache_metrics, collect, record_health):
            m.assert_not_called()  # no sample, no failure row, no health change, no SSH

    def test_fleet_metrics_reports_skips_separately(self):
        from telemetry import collector

        devices = [dict(DEVICE, id=i, hostname=f"sw{i}") for i in (12, 13, 14)]
        mock.patch.object(collector, "list_enabled_devices", return_value=devices).start()
        mock.patch.object(collector, "insert_metric").start()
        mock.patch.object(collector, "last_success_at", return_value=None).start()
        mock.patch.object(collector, "collect_device", side_effect=lambda d: {
            "device_id": d["id"], "hostname": d["hostname"], "collected_at": NOW, "cpu_percent": 5, "memory_percent": 5,
            "memory_used_mb": 1, "memory_total_mb": 2, "uptime_seconds": 1, "source": "cli", "status": "success",
            "error": None, "duration_ms": 5}).start()
        self.acquire("health", device_id=13)
        change = self.acquire("apply", device_id=14)
        coord.set_change_in_progress(change.lease, "c", "j", 300)
        summary = collector.run_fleet_metrics()
        self.assertEqual({k: summary[k] for k in ("success", "failed", "skipped_busy", "skipped_change")},
                         {"success": 1, "failed": 0, "skipped_busy": 1, "skipped_change": 1})

    def test_topology_skip_records_no_failure(self):
        from topology import discovery

        collect = mock.patch.object(discovery, "collect_lldp_output").start()
        failure = mock.patch.object(discovery, "record_discovery_failure").start()
        success = mock.patch.object(discovery, "record_discovery_success").start()
        holder = self.acquire("telemetry")
        result = discovery.discover_device(DEVICE, [], [])
        self.assertEqual((result["success"], result["skipped"]), (False, "skipped_busy"))
        holder.lease.release()
        for m in (collect, failure, success):
            m.assert_not_called()

    def test_topology_sweep_counts_skips_not_failures(self):
        from topology import discovery

        devices = [dict(DEVICE, id=i, hostname=f"sw{i}") for i in (12, 13)]
        mock.patch.object(discovery, "list_enabled_devices", return_value=devices).start()
        mock.patch.object(discovery, "list_inventory", return_value=[]).start()
        mock.patch.object(discovery, "list_lldp_identities", return_value=[]).start()
        mock.patch.object(discovery, "collect_lldp_output", return_value=("show lldp neighbors", "x")).start()
        mock.patch.object(discovery, "parse_lldp_output", return_value=([], [])).start()
        mock.patch.object(discovery, "record_discovery_success", return_value={"new": 0, "deactivated": 0}).start()
        mock.patch.object(discovery, "cache_topology_last_discovery").start()
        change = self.acquire("apply", device_id=13)
        coord.set_change_in_progress(change.lease, "c", "j", 300)
        summary = discovery.run_fleet_topology_discovery()
        self.assertEqual((summary["successful"], summary["failed"], summary["skipped_change"]), (1, 0, 1))
        self.assertEqual(summary["failed_devices"], [])


class QueueBehaviourTests(Backend, unittest.TestCase):
    def test_busy_devices_are_skipped_immediately_not_queued(self):
        from telemetry import collector

        devices = [dict(DEVICE, id=i, hostname=f"sw{i}") for i in range(20, 30)]
        mock.patch.object(collector, "list_enabled_devices", return_value=devices).start()
        collect = mock.patch.object(collector, "collect_device").start()
        for d in devices:
            self.acquire("topology", device_id=d["id"])
        started = time.monotonic()
        summary = collector.run_fleet_metrics()
        self.assertLess(time.monotonic() - started, 1.0)  # no waiting, no retries
        self.assertEqual(summary["skipped_busy"], 10)
        collect.assert_not_called()

    def test_beat_still_drops_late_sweeps(self):
        import worker

        expires = {k: v["options"]["expires"] for k, v in worker.app.conf.beat_schedule.items()}
        self.assertEqual(expires, {"fleet-health-every-60s": 55, "fleet-metrics-every-60s": 55,
                                   "topology-discovery-every-5m": 280, "pcap-cleanup-hourly": 3000})


# ---- manual backup + change reentrancy ------------------------------------------------------------
class BackupCoordinationTests(Backend, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from tasks import config_backup

        self.cb = config_backup
        p = lambda name, **kw: mock.patch.object(config_backup, name, **kw).start()
        p("mark_job_running", return_value=True)
        p("get_device_by_id", return_value=dict(DEVICE))
        self.failed = p("mark_job_failed")
        self.audit = p("create_audit_event")
        self.perform = p("perform_config_backup", return_value={"backup_id": 1, "created_at": None, "storage_path": "/x",
                                                                "checksum": "c", "size_bytes": 1})

    def test_manual_backup_takes_and_releases_the_slot(self):
        seen = {}
        self.perform.side_effect = lambda *a, **k: seen.update(op=coord.current_operation(12)["operation"]) or {
            "backup_id": 1, "created_at": None, "storage_path": "/x", "checksum": "c", "size_bytes": 1}
        self.assertEqual(self.cb.run_manual_backup(12, "job-1")["status"], "success")
        self.assertEqual(seen["op"], "backup")
        self.assertIsNone(coord.current_operation(12))

    def test_manual_backup_rejected_during_change(self):
        change = self.acquire("apply")
        coord.set_change_in_progress(change.lease, "c", "j", 300)
        result = self.cb.run_manual_backup(12, "job-2")
        self.assertEqual(result["error"], "Configuration change in progress on Kenda-Core-1; backup not started")
        self.failed.assert_called_once_with("job-2", result["error"])
        self.perform.assert_not_called()

    def test_manual_backup_waits_for_collector_then_runs(self):
        telemetry = self.acquire("telemetry")
        threading.Timer(0.3, telemetry.lease.release).start()
        self.assertEqual(self.cb.run_manual_backup(12, "job-3")["status"], "success")

    def test_manual_backup_fails_closed_without_redis(self):
        self.client.down = True if not self.real else None
        result = self.cb.run_manual_backup(12, "job-4")
        self.assertEqual(result["error"], "Device coordination unavailable (Redis); backup not started")
        self.perform.assert_not_called()


class ApplyCoordinationTests(Backend, unittest.TestCase):
    """The change workflow owns one slot for backup + apply + postcheck + health (no deadlock)."""

    def setUp(self):
        super().setUp()
        from changes import blocks as B
        from changes import platforms, workflow
        from test_change_workflow import MULTI, ApplyTests

        self.workflow, self.B = workflow, B
        p = lambda name, **kw: mock.patch.object(workflow, name, **kw).start()
        self.approval = {"approval_id": "a1", "device_id": 12, "backup_job_id": "b1", "status": "applying",
                         "approved_by": "r", "config_blocks": B.normalize_blocks(MULTI), "config_lines": ["x"],
                         "config_parents": None, "verification_commands": [], "execution_result": None}
        p("get_change_approval", return_value=self.approval)
        p("get_device_by_id", return_value=dict(DEVICE))
        p("verify_backup_for_device", return_value={"valid": True})
        p("begin_execution", return_value=True)
        p("set_execution_result")
        p("create_job", return_value="job-1")
        p("mark_job_success")
        p("mark_job_failed")
        self.audit = p("create_audit_event")
        self.applied, self.failed = p("mark_approval_applied"), p("mark_approval_failed")
        self.release_claim = p("release_apply_claim", return_value=True)
        self.seen = []
        record = lambda phase: (lambda *a, **k: self.seen.append((phase, (coord.current_operation(12) or {}).get("operation"),
                                                                    bool(coord.change_in_progress(12)))))
        self.backup = p("_pre_apply_backup", side_effect=lambda *a: record("backup")() or {"status": "success", "job_id": "b2"})
        self.health = p("_post_change_health", side_effect=lambda *a: record("health")() or {"ok": True, "status": "healthy"})
        state = {"running_config": ApplyTests.APPLIED_CONFIG, "vlan_ids": None}
        mock.patch.object(platforms.Os10Platform, "read_device_state",
                          side_effect=lambda *a: record("postcheck")() or dict(state)).start()
        mock.patch.object(platforms.Os10Platform, "apply_blocks", side_effect=self._apply).start()
        self.acquire_spy = mock.patch.object(workflow.coord, "acquire_device_operation",
                                             wraps=coord.acquire_device_operation).start()

    def _apply(self, host, blocks):
        from ansible.blocks_runner import parse_marker
        from test_change_workflow import marker

        self.seen.append(("apply", coord.current_operation(12)["operation"], bool(coord.change_in_progress(12))))
        steps = self.B.apply_steps(blocks, "configure terminal")
        out = marker([s["i"] for s in steps])
        return steps, {"returncode": 0, "stdout": out, "stderr": "", "progress": parse_marker(out)}

    def test_whole_change_runs_under_one_slot_then_cleans_up(self):
        result = self.workflow.run_apply("a1", claimed_by_api=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual([s[0] for s in self.seen], ["backup", "apply", "postcheck", "health"])
        self.assertEqual({(s[1], s[2]) for s in self.seen}, {("apply", True)})  # one owner, change state set
        self.assertEqual(self.acquire_spy.call_count, 1)  # children never re-acquire (no deadlock)
        self.assertIsNone(coord.current_operation(12))
        self.assertIsNone(coord.change_in_progress(12))

    def test_failure_still_clears_coordination(self):
        mock.patch.object(self.workflow, "_pre_apply_backup", side_effect=self.workflow.ChangeError(
            "Pre-apply backup failed (Device unreachable); nothing was sent")).start()
        with self.assertRaisesRegex(self.workflow.ChangeError, "Pre-apply backup failed"):
            self.workflow.run_apply("a1", claimed_by_api=True)
        self.failed.assert_called_once_with("a1")
        self.assertIsNone(coord.current_operation(12))
        self.assertIsNone(coord.change_in_progress(12))

    def test_busy_device_returns_approval_to_approved(self):
        holder = self.acquire("backup", ttl_seconds=600)
        with self.assertRaisesRegex(self.workflow.ChangeError, r"Apply not started: Kenda-Core-1 is busy \(backup\)\. "
                                                               r"Nothing was sent; the approval is back to approved"):
            self.workflow.run_apply("a1", claimed_by_api=True)
        self.release_claim.assert_called_once_with("a1")
        self.failed.assert_not_called()
        self.assertEqual(self.seen, [])
        self.assertIn("apply_deferred", [c.kwargs["event_type"] for c in self.audit.call_args_list])
        self.assertEqual(coord.current_operation(12)["operation"], "backup")  # untouched
        holder.lease.release()

    def test_apply_fails_closed_without_redis(self):
        self.client.down = True if not self.real else None
        with self.assertRaisesRegex(self.workflow.ChangeError, "device coordination is unavailable"):
            self.workflow.run_apply("a1", claimed_by_api=True)
        self.release_claim.assert_called_once_with("a1")
        self.assertEqual(self.seen, [])

    def test_post_change_health_failure_is_a_separate_warning(self):
        self.health.side_effect = lambda *a: {"ok": False, "status": "healthy", "error": "SSH authentication failed"}
        result = self.workflow.run_apply("a1", claimed_by_api=True)
        self.assertEqual(result["status"], "applied")  # the change result is preserved
        self.assertEqual(result["execution"]["post_change_health"]["error"], "SSH authentication failed")
        self.assertIn("post_change_health_warning", [c.kwargs["event_type"] for c in self.audit.call_args_list])

    def test_precheck_takes_the_slot_and_fails_closed_when_busy(self):
        from test_change_workflow import MULTI

        mock.patch.object(self.workflow, "get_device_by_hostname", return_value=dict(DEVICE)).start()
        change = self.acquire("apply")
        coord.set_change_in_progress(change.lease, "c", "j", 300)
        with self.assertRaisesRegex(self.workflow.ChangeError,
                                    "Precheck not started: a configuration change is in progress on Kenda-Core-1"):
            self.workflow.run_precheck("Kenda-Core-1", config_blocks=MULTI, backup=mock.Mock())
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
