"""
Backup core + manual backup task. No devices, Vault or DB: collectors and DB helpers are
mocked. Needs the worker's dependencies (run inside the worker image):
    python -m unittest discover -s tests -p 'test_config_backup.py' -v
"""

import hashlib
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tasks import config_backup as cb  # noqa: E402

CONFIG = "! test running-config\nhostname Kenda-HARO-IDF-A\n!\n"
JOB = "11111111-1111-1111-1111-111111111111"


def ok_result(host, config=CONFIG):
    return {host: {"failed": False, "timestamp": "20260930T120000Z",
                   "checksum": hashlib.sha256(config.encode()).hexdigest(), "config": config}}


from fakes import fake_redis  # noqa: E402

class CoreTestCase(unittest.TestCase):
    def setUp(self):
        fake_redis.install(self)  # in-memory device coordination
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = {
            name: mock.patch.object(cb, name, autospec=True).start()
            for name in ("create_audit_event", "create_backup_record", "mark_job_success",
                         "mark_job_failed", "mark_job_running", "get_device_by_id")
        }
        self.db["create_backup_record"].return_value = {"backup_id": 42, "created_at": "2026-09-30T12:00:00+00:00"}
        self.db["mark_job_running"].return_value = True
        mock.patch.object(cb, "BACKUP_ROOT", self.tmp.name).start()
        self.os6 = mock.Mock(side_effect=lambda host: ok_result(host))
        self.os10 = mock.Mock(side_effect=lambda host: ok_result(host))
        mock.patch.dict(cb.COLLECTORS, {"dell_os6": self.os6, "dell_os10": self.os10}).start()
        self.addCleanup(mock.patch.stopall)

    def audit_types(self):
        return [c.kwargs["event_type"] for c in self.db["create_audit_event"].call_args_list]


class PerformConfigBackupTests(CoreTestCase):
    def test_snapshot_path_reuses_config_without_device_contact(self):
        result = cb.perform_config_backup("Kenda-HARO-IDF-A", 1, "dell_os6", JOB, config_snapshot=CONFIG)

        self.os6.assert_not_called()
        self.assertTrue(result["storage_path"].startswith(f"{self.tmp.name}/Kenda-HARO-IDF-A/"))
        self.assertTrue(result["storage_path"].endswith(".cfg"))
        with open(result["storage_path"]) as f:
            self.assertEqual(f.read(), CONFIG)
        self.assertEqual(stat.S_IMODE(os.stat(result["storage_path"]).st_mode), 0o600)
        self.assertEqual(result["checksum"], hashlib.sha256(CONFIG.encode()).hexdigest())
        self.assertEqual(result["backup_id"], 42)
        self.db["mark_job_success"].assert_called_once_with(JOB)
        # Change-workflow audit text is unchanged (no source suffix).
        msgs = [c.kwargs["message"] for c in self.db["create_audit_event"].call_args_list]
        self.assertEqual(msgs[0], "Running-config backup started for Kenda-HARO-IDF-A")
        self.assertEqual(msgs[1], f"Backup completed: {result['storage_path']}")

    def test_os6_and_os10_use_their_existing_collectors(self):
        cb.perform_config_backup("Kenda-HARO-IDF-A", 1, "dell_os6", JOB)
        cb.perform_config_backup("Kenda-Core-1", 12, "dell_os10", JOB)
        self.os6.assert_called_once_with("Kenda-HARO-IDF-A")
        self.os10.assert_called_once_with("Kenda-Core-1")

    def test_real_collectors_are_the_platform_modules(self):
        mock.patch.stopall()
        from tasks import dell_os6, dell_os10
        self.assertIs(cb.COLLECTORS["dell_os6"], dell_os6.backup_running_config)
        self.assertIs(cb.COLLECTORS["dell_os10"], dell_os10.backup_running_config)

    def test_device_failure_raises_and_writes_nothing(self):
        self.os6.side_effect = lambda host: {host: {"failed": True, "result": "Command failed after 3 attempts."}}
        with self.assertRaisesRegex(RuntimeError, "Command failed after 3 attempts"):
            cb.perform_config_backup("Kenda-HARO-IDF-A", 1, "dell_os6", JOB)
        self.db["create_backup_record"].assert_not_called()
        self.db["mark_job_success"].assert_not_called()
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_unsupported_platform(self):
        with self.assertRaises(cb.UnsupportedPlatformError):
            cb.perform_config_backup("x", 1, "cisco_ios", JOB)

    def test_storage_error_keeps_original_message(self):
        with mock.patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(cb.BackupStorageError) as ctx:
                cb.perform_config_backup("Kenda-HARO-IDF-A", 1, "dell_os6", JOB)
        self.assertEqual(str(ctx.exception), str(PermissionError(13, "Permission denied")))
        self.assertEqual(cb.classify_backup_error(ctx.exception), "Backup storage error")


class ManualBackupTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.db["get_device_by_id"].return_value = {
            "id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "10.0.0.31",
            "platform": "dell_os6", "credential_path": "x",
        }

    def test_success_uses_shared_core_and_returns_backup_identity(self):
        with mock.patch.object(cb, "perform_config_backup", wraps=cb.perform_config_backup) as core:
            result = cb.run_manual_backup(1, JOB)

        core.assert_called_once_with("Kenda-HARO-IDF-A", 1, "dell_os6", JOB, source="manual")
        self.os6.assert_called_once_with("Kenda-HARO-IDF-A")
        self.db["mark_job_running"].assert_called_once_with(JOB)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["backup_id"], 42)
        self.assertNotIn("config", result)
        self.assertNotIn("storage_path", result)
        self.assertEqual(self.audit_types(), ["backup_started", "backup_completed"])
        self.assertIn("source=manual, backup_id=42", self.db["create_audit_event"].call_args_list[1].kwargs["message"])

    def test_auth_failure_records_sanitized_failed_job_without_raising(self):
        raw = "NetmikoAuthenticationException: Authentication to device failed.\nCommon causes ... user=admin"
        self.os6.side_effect = lambda host: {host: {"failed": True, "result": raw}}

        result = cb.run_manual_backup(1, JOB)

        self.assertEqual(result, {"status": "failed", "job_id": JOB, "device_id": 1, "error": "SSH authentication failed"})
        self.db["mark_job_failed"].assert_called_once_with(JOB, "SSH authentication failed")
        self.db["mark_job_success"].assert_not_called()
        self.assertEqual(self.audit_types(), ["backup_started", "backup_failed"])
        failed_msg = self.db["create_audit_event"].call_args_list[-1].kwargs["message"]
        self.assertNotIn("admin", failed_msg)
        self.assertIn("source=manual", failed_msg)

    def test_unreachable_device(self):
        self.os6.side_effect = lambda host: {host: {"failed": True, "result": (
            "Command failed after 3 attempts. Last error: TCP connection to device failed.")}}
        self.assertEqual(cb.run_manual_backup(1, JOB)["error"], "Device unreachable")

    def test_disabled_or_missing_device(self):
        self.db["get_device_by_id"].side_effect = ValueError("Enabled device not found: 1")
        result = cb.run_manual_backup(1, JOB)
        self.assertEqual(result["error"], "Device not found or disabled")
        self.os6.assert_not_called()

    def test_unsupported_platform_device(self):
        self.db["get_device_by_id"].return_value["platform"] = "cisco_ios"
        self.assertEqual(cb.run_manual_backup(1, JOB)["error"], "Unsupported platform")

    def test_job_not_queued_is_skipped_without_device_contact(self):
        self.db["mark_job_running"].return_value = False
        result = cb.run_manual_backup(1, JOB)
        self.assertEqual(result["status"], "skipped")
        self.db["get_device_by_id"].assert_not_called()
        self.os6.assert_not_called()
        self.db["mark_job_failed"].assert_not_called()


class ClassifyTests(unittest.TestCase):
    def test_categories(self):
        import hvac.exceptions

        cases = [
            (hvac.exceptions.Forbidden("permission denied"), "Credential lookup failed"),
            (RuntimeError("Unable to connect to port 22"), "Device unreachable"),
            (RuntimeError("Pattern not detected: 'x' in output. Things you might try..."), "Running-config command failed"),
            (ValueError("Device not found in inventory: X"), "Device missing from worker inventory"),
        ]
        for exc, expected in cases:
            with self.subTest(exc=exc):
                self.assertEqual(cb.classify_backup_error(exc), expected)


class WorkerTaskTests(unittest.TestCase):
    """The Celery tasks in worker.py: change-workflow shape unchanged, manual task wiring."""

    @classmethod
    def setUpClass(cls):
        import worker

        cls.worker = worker

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        fake_redis.install(self)  # in-memory device coordination
        w = self.worker
        mock.patch.object(w, "get_device_by_hostname", return_value={
            "id": 1, "hostname": "Kenda-HARO-IDF-A", "platform": "dell_os6"}).start()
        self.create_job = mock.patch.object(w, "create_job", return_value=JOB).start()
        self.mark_failed = mock.patch.object(w, "mark_job_failed").start()
        self.audit = mock.patch.object(w, "create_audit_event").start()

    def test_change_workflow_backup_result_shape_unchanged(self):
        core = mock.patch.object(self.worker, "perform_config_backup", return_value={
            "backup_id": 5, "created_at": "t", "storage_path": "/backups/Kenda-HARO-IDF-A/x.cfg",
            "checksum": "abc", "size_bytes": 3}).start()

        result = self.worker.backup_running_config_task("Kenda-HARO-IDF-A", config_snapshot=CONFIG)

        self.create_job.assert_called_once_with(device_id=1, job_type="config_backup", requested_by="celery")
        core.assert_called_once_with("Kenda-HARO-IDF-A", 1, "dell_os6", JOB, config_snapshot=CONFIG)
        self.assertEqual(result, {
            "job_id": JOB, "status": "success", "hostname": "Kenda-HARO-IDF-A", "platform": "dell_os6",
            "storage_path": "/backups/Kenda-HARO-IDF-A/x.cfg", "checksum": "abc"})

    def test_change_workflow_backup_failure_still_fails_job_and_raises(self):
        mock.patch.object(self.worker, "perform_config_backup", side_effect=RuntimeError("boom")).start()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.worker.backup_running_config_task("Kenda-HARO-IDF-A")
        self.mark_failed.assert_called_once_with(JOB, "boom")
        self.assertEqual(self.audit.call_args.kwargs["event_type"], "backup_failed")

    def test_precheck_backs_up_its_snapshot_before_approval(self):
        # The OS6 precheck now runs through the shared workflow (changes/workflow.py) and the
        # OS6 platform adapter, which still uses ansible.run_os6's reader and verifier.
        import ansible.run_os6 as run_os6
        from changes import workflow

        w = self.worker
        mock.patch.object(run_os6, "get_show_output", return_value=CONFIG).start()
        mock.patch.object(run_os6, "run_os6_config_check",
                          return_value={"would_change": True, "proposed_changes": ["vlan 200"]}).start()
        mock.patch.object(workflow, "get_device_by_hostname",
                          return_value={"id": 1, "hostname": "Kenda-HARO-IDF-A", "platform": "dell_os6"}).start()
        mock.patch.object(workflow, "create_audit_event").start()
        approval = mock.patch.object(workflow, "create_change_approval", return_value={"approval_id": "a", "status": "pending"}).start()
        backup = mock.patch.object(w, "backup_running_config_task", return_value={"status": "success", "job_id": JOB}).start()

        result = w.os6_change_precheck("Kenda-HARO-IDF-A", ["vlan 200"])

        backup.assert_called_once_with("Kenda-HARO-IDF-A", config_snapshot=CONFIG)
        self.assertEqual(approval.call_args.kwargs["backup_job_id"], JOB)
        self.assertEqual(result["status"], "pending_approval")

    def test_manual_task_releases_lock_even_on_unexpected_error(self):
        mock.patch.object(self.worker, "run_manual_backup", side_effect=RuntimeError("db down")).start()
        release = mock.patch.object(self.worker, "release_device_backup_lock").start()
        with self.assertRaises(RuntimeError):
            self.worker.manual_backup_device(1, JOB, lock_token=JOB)
        release.assert_called_once_with(1, JOB)

    def test_manual_task_delegates(self):
        run = mock.patch.object(self.worker, "run_manual_backup", return_value={"status": "success"}).start()
        mock.patch.object(self.worker, "release_device_backup_lock").start()
        self.assertEqual(self.worker.manual_backup_device("7", JOB, lock_token=JOB), {"status": "success"})
        run.assert_called_once_with(7, JOB)


if __name__ == "__main__":
    unittest.main()
