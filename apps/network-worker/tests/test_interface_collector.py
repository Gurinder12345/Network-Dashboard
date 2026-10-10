"""
Interface collector: real Netmiko SSH sessions against the simulated OS6/OS10 CLIs
(tests/fakes, NOT real switches), device coordination on the in-memory Redis, PostgreSQL
replaced by an in-memory store (the real SQL is covered by test_interface_db.py).

Covers: one SSH session per device, only `show` commands, partial collection, utilization
from the second sample, Redis snapshot semantics (written only after a stored collection;
skips/failures/empty data never overwrite it), coordination skips (change, precheck,
backup, health, telemetry, Redis down), lock release on errors, TTL crash recovery,
concurrency, and that NOTHING here ever changes device health.

    python -m unittest tests.test_interface_collector -v
"""

import json
import os
import sys
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import redis  # noqa: E402
from netmiko import ConnectHandler  # noqa: E402

from coordination import device_ops as coord  # noqa: E402
from fakes import fake_redis  # noqa: E402
from fakes.os6_device import FakeOS6  # noqa: E402
from fakes.os10_device import FakeOS10  # noqa: E402
from fakes.switch import TEST_PASSWORD, TEST_USERNAME  # noqa: E402
from health import cache as health_cache  # noqa: E402
from health import checks  # noqa: E402
from interfaces import cache, collector  # noqa: E402
from interfaces.parsers import INTERFACE_COMMANDS, OS6_COUNTERS, OS6_ERRORS  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "interfaces")
OS6_ERROR = "% Invalid input detected at '^' marker."


def fixture_outputs(platform):
    prefix = "os6" if platform == "dell_os6" else "os10"
    outputs = {}
    for command in INTERFACE_COMMANDS[platform]:
        with open(os.path.join(FIXTURES, f"{prefix}_{command.replace(' ', '_')}.txt")) as handle:
            outputs[command] = handle.read()
    return outputs


class InterfaceOS6(FakeOS6):
    def __init__(self, outputs, **kw):
        self.outputs = outputs
        super().__init__(**kw)

    def show(self, command):
        return self.outputs[command] if command in self.outputs else super().show(command)


class InterfaceOS10(FakeOS10):
    def __init__(self, outputs, **kw):
        self.outputs = outputs
        super().__init__(**kw)

    def show(self, command):
        return self.outputs[command] if command in self.outputs else super().show(command)


class MemoryStore:
    """Stands in for db.interfaces.persist_collection (same contract, no SQL)."""

    def __init__(self):
        self.ids = {}
        self.last = {}
        self.calls = 0
        self.fail = None

    def persist(self, device_id, collected_at, items, lookback_seconds, derive):
        self.calls += 1
        if self.fail:
            raise self.fail
        rows = []
        for item in items:
            interface_id = self.ids.setdefault((device_id, item["canonical_name"]), len(self.ids) + 1)
            derived = derive(self.last.get(interface_id), item)
            rows.append({**item, **derived, "id": interface_id, "first_seen": collected_at,
                         "last_state_change": item.get("last_state_change")})
            self.last[interface_id] = {"collected_at": collected_at, **{k: item.get(k) for k in (
                "rx_bytes", "tx_bytes", "rx_errors", "tx_errors", "crc_errors", "input_discards", "output_discards")}}
        return rows, 3


def device(id_, platform, hostname):
    return {"id": id_, "hostname": hostname, "platform": platform, "management_ip": "127.0.0.1", "credential_path": "test/x"}


OS6_DEV = device(1, "dell_os6", "Kenda-HARO-SW-01")
OS10_DEV = device(12, "dell_os10", "Kenda-Core-2")


REAL_REDIS = os.getenv("IFACE_TEST_REDIS_URL")


class Base(unittest.TestCase):
    real = False  # True: a disposable REAL Redis (IFACE_TEST_REDIS_URL) instead of the in-memory fake

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        if self.real:
            client = redis.Redis.from_url(REAL_REDIS, decode_responses=True)
            client.flushdb()
            self.redis = fake_redis.install(self, client)
        else:
            self.redis = fake_redis.install(self)
        mock.patch.object(cache, "_redis", return_value=self.redis).start()
        self.store = MemoryStore()
        mock.patch.object(collector, "persist_collection", side_effect=self.store.persist).start()
        self.lldp = mock.patch.object(collector, "lldp_managed_ports", return_value=[]).start()
        mock.patch.object(collector, "_tcp_reachable", return_value=(True, None)).start()
        mock.patch.object(collector, "get_device_credentials",
                          return_value={"username": TEST_USERNAME, "password": TEST_PASSWORD, "secret": ""}).start()
        # Health must never be written by interface collection, whatever happens.
        self.health_writes = [
            mock.patch("db.health.record_health").start(),
            mock.patch.object(checks, "record_health").start(),
            mock.patch.object(health_cache, "cache_device_health").start(),
            mock.patch.object(checks, "cache_device_health").start(),
        ]

    def tearDown(self):
        for write in self.health_writes:
            write.assert_not_called()
        self.assertFalse(self.redis.keys("health:*") if self.real else [k for k in self.redis.data if k.startswith("health:")])

    def snapshot(self, device_id):
        raw = self.redis.get(cache.SNAPSHOT_KEY.format(device_id=device_id))
        return json.loads(raw) if raw else None

    def attempt(self, device_id):
        raw = self.redis.get(cache.ATTEMPT_KEY.format(device_id=device_id))
        return json.loads(raw) if raw else None


class RealSshTests(Base):
    """Real Netmiko against the simulated CLIs."""

    def use(self, fake):
        self.addCleanup(fake.close)

        def open_session(dev, credentials):
            params = dict(host="127.0.0.1", port=fake.port, username=credentials["username"],
                          password=credentials["password"], secret="", conn_timeout=5, auth_timeout=10,
                          banner_timeout=10, fast_cli=False)
            if dev["platform"] == "dell_os6":
                return checks.HealthDellOS6SSH(**params)
            return ConnectHandler(device_type="dell_os10", **params)

        mock.patch.object(collector, "_open_session", side_effect=open_session).start()
        return fake

    def sent_commands(self, fake):
        return [line for line in fake.log if line.lower().startswith("show ")]

    def test_os6_one_session_show_commands_only_and_snapshot(self):
        fake = self.use(InterfaceOS6(fixture_outputs("dell_os6")))
        outcome = collector.collect_and_record(OS6_DEV)
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(fake.connections, 1)                                     # ONE SSH session
        self.assertEqual(self.sent_commands(fake), list(INTERFACE_COMMANDS["dell_os6"]))
        self.assertEqual(fake.config_writes(), [])                               # never configuration mode
        self.assertFalse([l for l in fake.log if "clear" in l.lower()])

        snap = self.snapshot(1)
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(snap["collection_status"], "success")
        self.assertEqual(snap["summary"], {"total": 8, "up": 6, "down": 1, "admin_down": 1, "unknown": 0,
                                           "erroring": 0, "trunks": 3, "access_ports": 4})
        self.assertAlmostEqual(self.redis.ttl(cache.SNAPSHOT_KEY.format(device_id=1)), collector.CACHE_TTL_SECONDS, delta=2)
        gi = next(i for i in snap["interfaces"] if i["canonical_name"] == "gi1/0/1")
        self.assertIsNone(gi["rx_utilization_pct"])                                # first sample: null, not 0
        self.assertEqual(self.attempt(1)["status"], "success")

    def test_second_sample_gives_utilization_and_error_deltas(self):
        outputs = fixture_outputs("dell_os6")
        fake = self.use(InterfaceOS6(outputs))
        first = collector.collect_and_record(OS6_DEV)
        self.assertEqual(first["status"], "success")
        # Move the stored "previous" sample 300 s back, then raise Gi1/0/1's counters.
        for sample in self.store.last.values():
            sample["collected_at"] -= timedelta(seconds=300)
        fake.outputs[OS6_COUNTERS] = outputs[OS6_COUNTERS].replace("812345678", str(812345678 + 3_750_000_000))
        fake.outputs[OS6_ERRORS] = outputs[OS6_ERRORS].replace("Gi1/0/1   0          0", "Gi1/0/1   0          9")
        collector.collect_and_record(OS6_DEV)
        snap = self.snapshot(1)
        gi = next(i for i in snap["interfaces"] if i["canonical_name"] == "gi1/0/1")
        self.assertAlmostEqual(gi["rx_utilization_pct"], 10.0, delta=0.2)
        self.assertEqual(gi["crc_delta"], 9)
        self.assertTrue(gi["erroring"])
        self.assertEqual(snap["summary"]["erroring"], 1)
        self.assertEqual(fake.connections, 2)  # one per collection

    def test_os10_two_commands_one_session(self):
        fake = self.use(InterfaceOS10(fixture_outputs("dell_os10")))
        self.lldp.return_value = ["ethernet1/1/25"]  # topology: managed neighbor on this port
        outcome = collector.collect_and_record(OS10_DEV)
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(fake.connections, 1)
        self.assertEqual(self.sent_commands(fake), list(INTERFACE_COMMANDS["dell_os10"]))
        self.assertEqual(fake.config_writes(), [])
        snap = self.snapshot(12)
        roles = {i["canonical_name"]: i["role"] for i in snap["interfaces"]}
        self.assertEqual(roles["ethernet1/1/25"], "inter_switch")
        self.assertEqual(roles["port-channel10"], "lag")
        self.assertEqual(roles["ethernet1/1/30:2"], "uplink")   # its description says UPLINK
        self.assertIsNone(roles["ethernet1/1/5"])               # no evidence: neutral
        port = next(i for i in snap["interfaces"] if i["canonical_name"] == "ethernet1/1/18")
        self.assertIsNotNone(port["last_state_change"])          # device-reported "time since change"

    def test_unsupported_optional_command_is_partial_and_still_cached(self):
        outputs = fixture_outputs("dell_os6")
        outputs[OS6_ERRORS] = OS6_ERROR
        self.use(InterfaceOS6(outputs))
        outcome = collector.collect_and_record(OS6_DEV)
        self.assertEqual(outcome["status"], "partial")
        snap = self.snapshot(1)
        self.assertEqual(snap["collection_status"], "partial")
        self.assertTrue(snap["problems"])
        self.assertIsNone(snap["interfaces"][0]["crc_errors"])

    def test_unparseable_device_fails_and_keeps_previous_snapshot(self):
        outputs = fixture_outputs("dell_os6")
        fake = self.use(InterfaceOS6(outputs))
        collector.collect_and_record(OS6_DEV)
        before = self.snapshot(1)
        fake.outputs = {c: OS6_ERROR for c in outputs}
        outcome = collector.collect_and_record(OS6_DEV)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(self.snapshot(1), before)                 # failed collector preserves cache
        self.assertEqual(self.attempt(1)["status"], "failed")
        self.assertEqual(self.store.calls, 1)                      # nothing written for the failure


class MockedSessionBase(Base):
    def setUp(self):
        super().setUp()
        self.outputs = {**fixture_outputs("dell_os6"), **fixture_outputs("dell_os10")}
        self.active = {}
        self.overlaps = []
        self.lock = threading.Lock()
        self.delay = 0
        self.opened = mock.patch.object(collector, "_open_session", side_effect=self.open_session).start()

    def open_session(self, dev, credentials):
        session = mock.Mock()

        def send(command, read_timeout):
            with self.lock:
                self.active[dev["id"]] = self.active.get(dev["id"], 0) + 1
                if self.active[dev["id"]] > 1:
                    self.overlaps.append(dev["id"])
            time.sleep(self.delay)
            with self.lock:
                self.active[dev["id"]] -= 1
            return self.outputs[command]

        session.send_command.side_effect = send
        return session


class CacheSemanticsTests(MockedSessionBase):
    def test_skipped_collector_preserves_cache(self):
        collector.collect_and_record(OS6_DEV)
        before = self.snapshot(1)
        holder = coord.acquire_device_operation(1, "telemetry")
        outcome = collector.collect_and_record(OS6_DEV)
        holder.lease.release()
        self.assertEqual(outcome["status"], "skipped_busy")
        self.assertEqual(self.snapshot(1), before)
        self.assertEqual(self.attempt(1)["status"], "skipped_busy")

    def test_database_failure_preserves_cache(self):
        collector.collect_and_record(OS6_DEV)
        before = self.snapshot(1)
        self.store.fail = RuntimeError("PostgreSQL down")
        outcome = collector.collect_and_record(OS6_DEV)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(self.snapshot(1), before)
        self.assertIn("Database write failed", self.attempt(1)["error"])
        self.assertEqual(coord.current_operation(1), None)  # lease released

    def test_redis_write_failure_keeps_postgresql_result(self):
        broken = mock.Mock(wraps=self.redis)
        broken.setex.side_effect = redis.ConnectionError("cache down")
        mock.patch.object(cache, "_redis", return_value=broken).start()
        outcome = collector.collect_and_record(OS6_DEV)
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(outcome["samples_written"], 8)  # PostgreSQL write kept
        self.assertFalse(outcome["cache_written"])

    def test_empty_snapshot_is_never_written(self):
        self.assertFalse(cache.write_snapshot({"device_id": 1, "interfaces": []}, 900))
        self.assertIsNone(self.snapshot(1))

    def test_time_budget_stops_further_commands(self):
        # 5.5 s budget, 0.3 s per command: two commands fit, the rest would cross the
        # 5 s floor and are not sent (the slot TTL can never expire mid-session).
        mock.patch.object(collector, "DEVICE_BUDGET_SECONDS", 5.5).start()
        self.delay = 0.3
        result = collector.collect_device(OS6_DEV)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["commands_run"], 2)
        self.assertIn("budget", " ".join(result["problems"]))


class CoordinationTests(MockedSessionBase):
    def held(self, operation, device_id=1):
        acq = coord.acquire_device_operation(device_id, operation, wait_seconds=0)
        self.assertTrue(acq.acquired)
        self.addCleanup(acq.lease.release)
        return acq.lease

    def assertSkipped(self, status, reason_part):
        outcome = collector.collect_and_record(OS6_DEV)
        self.assertEqual(outcome["status"], status)
        self.assertIn(reason_part, outcome["reason"])
        self.opened.assert_not_called()  # no SSH at all
        self.assertIsNone(self.snapshot(1))

    def test_apply_and_postcheck_skip_as_change(self):
        self.held("apply")  # the apply lease also covers backup, postcheck and post-change health
        self.assertSkipped("skipped_change", "change_in_progress")

    def test_settling_change_state_skips_even_without_a_slot_holder(self):
        lease = self.held("apply")
        coord.set_change_in_progress(lease, "c1", "j1", 600)
        lease.release()  # slot free, change state still present (settling)
        self.assertSkipped("skipped_change", "change_in_progress")

    def test_precheck_backup_health_telemetry_skip_as_busy(self):
        for operation in ("precheck", "backup", "health", "telemetry"):
            with self.subTest(operation=operation):
                lease = self.held(operation)
                self.assertSkipped("skipped_busy", f"busy:{operation}")
                lease.release()

    def test_interface_poll_blocks_topology_and_yields_to_waiting_health(self):
        lease = self.held("interface_poll")
        self.assertFalse(coord.acquire_device_operation(1, "topology").acquired)   # topology skips
        self.assertFalse(coord.acquire_device_operation(1, "telemetry").acquired)  # no preemption either
        lease.release()
        # A waiting health check (higher priority) makes a new interface poll yield.
        self.redis.set(coord.WAITING_KEY.format(device_id=1),
                       json.dumps({"token": "h", "priority": 2, "operation": "health"}), ex=15)
        self.assertSkipped("skipped_busy", "higher_priority_waiting:health")

    def test_redis_coordination_down_skips_without_ssh(self):
        if self.real:
            self.skipTest("outage simulated with the in-memory Redis")
        self.redis.down = True
        outcome = collector.collect_and_record(OS6_DEV)
        self.assertEqual(outcome["status"], "skipped_unavailable")
        self.opened.assert_not_called()

    def test_exception_releases_the_slot(self):
        mock.patch.object(collector, "collect_device", side_effect=RuntimeError("boom")).start()
        with self.assertRaises(RuntimeError):
            collector.collect_and_record(OS6_DEV)
        self.assertIsNone(coord.current_operation(1))

    def test_crashed_holder_expires_by_ttl(self):
        if self.real:
            # Real TTL: shorten it instead of waiting 150 s.
            coord.acquire_device_operation(1, "interface_poll", ttl_seconds=1)
            self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "skipped_busy")
            time.sleep(1.2)
            self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "success")
            return
        coord.acquire_device_operation(1, "interface_poll")  # never released (worker crash)
        self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "skipped_busy")
        self.redis.advance(coord.OPERATIONS["interface_poll"]["ttl"] + 1)
        self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "success")

    def test_same_device_never_two_sessions_different_devices_concurrent(self):
        self.delay = 0.15
        other = device(2, "dell_os6", "Kenda-HQ-SW-01")
        results = []
        threads = [threading.Thread(target=lambda d=d: results.append(collector.collect_and_record(d)))
                   for d in (OS6_DEV, OS6_DEV, other)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        statuses = sorted(r["status"] for r in results)
        self.assertEqual(statuses, ["skipped_busy", "success", "success"])
        self.assertEqual(self.overlaps, [])
        self.assertEqual(self.opened.call_count, 2)  # device 1 once, device 2 once (in parallel)

    def test_health_isolation_on_every_failure_path(self):
        # SSH/TCP failure, parser failure, skip, Redis cache failure, PostgreSQL failure:
        # tearDown asserts no health write happened for any of them.
        with mock.patch.object(collector, "_tcp_reachable", return_value=(False, "timeout")):
            self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "failed")
        self.outputs = {k: "garbage" for k in self.outputs}
        self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "failed")
        self.outputs = {**fixture_outputs("dell_os6")}
        with mock.patch.object(cache, "write_snapshot", side_effect=lambda *a: False):
            self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "success")
        self.store.fail = RuntimeError("db")
        self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "failed")
        self.held("health")
        self.assertEqual(collector.collect_and_record(OS6_DEV)["status"], "skipped_busy")


class FleetTests(MockedSessionBase):
    def test_fleet_orders_os10_first_and_counts(self):
        devices = [OS6_DEV, device(2, "dell_os6", "Kenda-HQ-SW-01"), OS10_DEV]
        mock.patch.object(collector, "list_enabled_devices", return_value=devices).start()
        order = []
        original = collector.collect_and_record
        mock.patch.object(collector, "collect_and_record",
                          side_effect=lambda d, t: (order.append(d["platform"]), original(d, t))[1]).start()
        mock.patch.object(collector, "CONCURRENCY", 1).start()
        summary = collector.run_fleet_interfaces()
        self.assertEqual(order[0], "dell_os10")
        self.assertEqual((summary["success"], summary["devices_total"], summary["interfaces_seen"]), (3, 3, 8 + 8 + 8))

    def test_overlapping_sweep_is_skipped_not_queued(self):
        self.redis.set(collector.FLEET_LOCK_KEY, "other", ex=60)
        mock.patch.object(collector, "list_enabled_devices", return_value=[OS6_DEV]).start()
        self.assertTrue(collector.run_fleet_interfaces()["skipped"])
        self.opened.assert_not_called()


@unittest.skipUnless(REAL_REDIS, "IFACE_TEST_REDIS_URL not set (disposable Redis)")
class RealRedisCacheTests(CacheSemanticsTests):
    real = True

    def test_snapshot_ttl_and_attempt_on_real_redis(self):
        collector.collect_and_record(OS6_DEV)
        ttl = self.redis.ttl(cache.SNAPSHOT_KEY.format(device_id=1))
        self.assertTrue(collector.CACHE_TTL_SECONDS - 2 <= ttl <= collector.CACHE_TTL_SECONDS)
        self.assertGreater(self.redis.ttl(cache.ATTEMPT_KEY.format(device_id=1)), 86000)
        # One key per device for the snapshot (plus one attempt key) -- not one per interface.
        self.assertEqual(sorted(self.redis.keys("interface:*")), [cache.ATTEMPT_KEY.format(device_id=1),
                                                                   cache.SNAPSHOT_KEY.format(device_id=1)])


@unittest.skipUnless(REAL_REDIS, "IFACE_TEST_REDIS_URL not set (disposable Redis)")
class RealRedisCoordinationTests(CoordinationTests):
    real = True


if __name__ == "__main__":
    unittest.main()
