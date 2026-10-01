"""
Telemetry collection pipeline (no devices, Vault, DB or Redis: all mocked). The parsers
themselves are tested separately against real fixtures; here a stub parser stands in so
the result semantics, failure handling and fleet concurrency are covered. Run inside the
worker image:
    python -m unittest tests.test_telemetry_collector -v
"""

import os
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException  # noqa: E402

from telemetry import collector, parsers  # noqa: E402

OS6 = {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "10.0.0.31", "platform": "dell_os6", "credential_path": "x"}
FULL = {"cpu_percent": 12.5, "memory_percent": 41.0, "memory_used_mb": 820.0, "memory_total_mb": 2000.0, "uptime_seconds": 86400}


class CollectorTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(parsers.TELEMETRY_COMMANDS, {"dell_os6": ("show test-cpu", "show test-mem")}, clear=True).start()
        self.parser = mock.Mock(return_value=dict(FULL))
        mock.patch.dict(parsers.PARSERS, {"dell_os6": self.parser}, clear=True).start()
        self.tcp = mock.patch.object(collector, "_tcp_reachable", return_value=(True, None)).start()
        self.creds = mock.patch.object(collector, "get_device_credentials",
                                       return_value={"username": "u", "password": "pw-secret", "secret": "en-secret"}).start()
        self.session = mock.Mock()
        self.session.send_command.side_effect = lambda cmd, read_timeout: f"output of {cmd}"
        self.open = mock.patch.object(collector, "_open_session", return_value=self.session).start()


class CollectDeviceTests(CollectorTestCase):
    def test_success_runs_validated_commands_in_one_session(self):
        sample = collector.collect_device(OS6)

        self.assertEqual(sample["status"], "success")
        self.assertIsNone(sample["error"])
        self.assertEqual(sample["cpu_percent"], 12.5)
        self.assertEqual(sample["uptime_seconds"], 86400)
        self.assertEqual(sample["source"], "cli:show test-cpu; show test-mem")
        self.open.assert_called_once()
        self.assertEqual([c.args[0] for c in self.session.send_command.call_args_list], ["show test-cpu", "show test-mem"])
        self.parser.assert_called_once_with({"show test-cpu": "output of show test-cpu", "show test-mem": "output of show test-mem"})
        self.session.disconnect.assert_called_once()

    def test_only_show_commands_are_sent(self):
        collector.collect_device(OS6)
        for call in self.session.send_command.call_args_list:
            self.assertTrue(call.args[0].startswith("show "))
        self.assertFalse(self.session.send_config_set.called)

    def test_partial_when_one_metric_missing(self):
        self.parser.return_value = {**FULL, "memory_percent": None}
        sample = collector.collect_device(OS6)
        self.assertEqual((sample["status"], sample["error"]), ("partial", "Memory unavailable"))
        self.assertEqual(sample["cpu_percent"], 12.5)

        self.parser.return_value = {**FULL, "cpu_percent": None}
        self.assertEqual(collector.collect_device(OS6)["error"], "CPU unavailable")

    def test_platform_without_validated_commands_never_connects(self):
        parsers.TELEMETRY_COMMANDS.clear()
        sample = collector.collect_device(OS6)
        self.assertEqual(sample["status"], "failed")
        self.assertIn("not yet validated", sample["error"])
        self.tcp.assert_not_called()
        self.open.assert_not_called()

    def test_unreachable_fails_without_vault_or_ssh(self):
        self.tcp.return_value = (False, "TCP 22 unreachable: timeout")
        sample = collector.collect_device(OS6)
        self.assertEqual((sample["status"], sample["error"]), ("failed", "Device unreachable (TCP 22)"))
        self.creds.assert_not_called()
        self.open.assert_not_called()
        self.assertIsNone(sample["cpu_percent"])

    def test_auth_failure(self):
        self.open.side_effect = NetmikoAuthenticationException("Authentication to device failed.")
        self.assertEqual(collector.collect_device(OS6)["error"], "SSH authentication failed")

    def test_ssh_and_cli_failures_are_sanitized(self):
        self.open.side_effect = NetmikoTimeoutException("TCP connection to device failed. pw-secret")
        error = collector.collect_device(OS6)["error"]
        self.assertEqual(error, "SSH session failed: NetmikoTimeoutException")

        self.open.side_effect = None
        self.session.send_command.side_effect = OSError("socket closed")
        self.assertEqual(collector.collect_device(OS6)["error"], "CLI command failed: OSError")
        self.session.disconnect.assert_called()

    def test_vault_failure(self):
        self.creds.side_effect = RuntimeError("permission denied")
        self.assertEqual(collector.collect_device(OS6)["error"], "Credential lookup failed: RuntimeError")

    def test_parser_failure_never_fabricates_values(self):
        self.parser.side_effect = parsers.TelemetryParseError("CPU table not found")
        sample = collector.collect_device(OS6)
        self.assertEqual(sample["status"], "failed")
        self.assertEqual(sample["error"], "Telemetry output could not be parsed: CPU table not found")
        self.assertEqual([sample[f] for f in parsers.FIELDS], [None] * 5)

    def test_out_of_range_and_incomplete_parser_output_rejected(self):
        self.parser.return_value = {**FULL, "cpu_percent": 140.0}
        self.assertIn("out of range", collector.collect_device(OS6)["error"])
        self.parser.return_value = {"cpu_percent": 3.0}
        self.assertIn("omitted fields", collector.collect_device(OS6)["error"])

    def test_neither_metric_is_failed(self):
        self.parser.return_value = {f: None for f in parsers.FIELDS}
        self.assertEqual(collector.collect_device(OS6)["status"], "failed")


class RecordTests(CollectorTestCase):
    def setUp(self):
        super().setUp()
        self.insert = mock.patch.object(collector, "insert_metric", return_value=1).start()
        self.cache = mock.patch.object(collector, "cache_device_metrics").start()
        self.last = mock.patch.object(collector, "last_success_at").start()

    def test_success_caches_latest_with_its_own_time_as_last_success(self):
        sample = collector.collect_and_record(OS6)
        self.insert.assert_called_once_with(sample)
        payload = self.cache.call_args.args[0]
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["last_success_at"], payload["collected_at"])
        self.last.assert_not_called()
        self.assertNotIn("source", payload)

    def test_failure_stores_nulls_and_keeps_previous_last_success(self):
        self.tcp.return_value = (False, "x")
        earlier = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        self.last.return_value = earlier

        collector.collect_and_record(OS6)

        stored = self.insert.call_args.args[0]
        self.assertEqual((stored["status"], stored["cpu_percent"], stored["memory_percent"]), ("failed", None, None))
        payload = self.cache.call_args.args[0]
        self.assertEqual(payload["last_success_at"], earlier.isoformat())
        self.assertEqual(payload["error"], "Device unreachable (TCP 22)")


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.devices = [{**OS6, "id": i, "hostname": f"sw{i}"} for i in range(10)]
        mock.patch.object(collector, "list_enabled_devices", return_value=self.devices).start()
        self.lock = mock.MagicMock()
        self.lock.__enter__.return_value.acquired = True
        mock.patch.object(collector, "FleetLock", return_value=self.lock).start()

    def test_bounded_concurrency_and_one_failure_does_not_stop_fleet(self):
        active, peak, guard = [0], [0], threading.Lock()

        def fake(device):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with guard:
                active[0] -= 1
            if device["id"] == 3:
                raise RuntimeError("db down")
            status = "failed" if device["id"] in (7, 8, 9) else "success"
            return {"status": status, "duration_ms": 10}

        with mock.patch.object(collector, "collect_and_record", side_effect=fake):
            summary = collector.run_fleet_metrics(trigger="manual")

        self.assertLessEqual(peak[0], collector.METRICS_CONCURRENCY)
        self.assertGreater(peak[0], 1)
        self.assertEqual((summary["devices_total"], summary["success"], summary["failed"], summary["not_recorded"]), (10, 6, 3, 1))
        collector.FleetLock.assert_called_once_with(key="metrics:fleet:lock", ttl_seconds=collector.METRICS_LOCK_TTL_SECONDS)

    def test_lock_held_skips_run(self):
        self.lock.__enter__.return_value.acquired = False
        with mock.patch.object(collector, "collect_and_record") as run:
            summary = collector.run_fleet_metrics()
        self.assertTrue(summary["skipped"])
        run.assert_not_called()


class BeatScheduleTests(unittest.TestCase):
    def test_all_three_schedules(self):
        import worker

        schedule = worker.app.conf.beat_schedule
        self.assertTrue(worker.TELEMETRY_BEAT_ENABLED)
        self.assertEqual(set(schedule), {"fleet-health-every-60s", "topology-discovery-every-5m", "fleet-metrics-every-60s"})
        # Cadence and expiry; second offsets are covered in test_beat_schedule.py.
        expected = {
            "fleet-health-every-60s": ("network_worker.health_check_all_devices", 60, 55),
            "topology-discovery-every-5m": ("network_worker.discover_topology_all_devices", 300, 280),
            # A telemetry run that cannot start within 55 s is dropped (no backlog).
            "fleet-metrics-every-60s": ("network_worker.collect_fleet_metrics", 60, 55),
        }
        for name, (task, period, expires) in expected.items():
            entry = schedule[name]
            self.assertEqual((entry["task"], entry["schedule"].seconds, entry["options"], entry["kwargs"]),
                             (task, period, {"expires": expires}, {"trigger": "schedule"}))
        for entry in schedule.values():
            self.assertIn(entry["task"], worker.app.tasks)
        self.assertIn("network_worker.collect_device_metrics", worker.app.tasks)

    def test_fleet_lock_settings_unchanged(self):
        # Lock outlives a worst-case run but is released at the end of every run.
        self.assertEqual(collector.METRICS_LOCK_KEY, "metrics:fleet:lock")
        self.assertEqual(collector.METRICS_LOCK_TTL_SECONDS, 300)
        self.assertEqual(collector.METRICS_CONCURRENCY, 4)
        from health import cache
        self.assertEqual(cache.METRICS_DEVICE_TTL_SECONDS, 180)


if __name__ == "__main__":
    unittest.main()
