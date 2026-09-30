"""
Manual backup trigger, job status and backup download. DB, Redis and Celery are mocked;
no switch, Vault or database is contacted. Needs fastapi + httpx. Run from apps/network-api:
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import backup_download, device_backup  # noqa: E402
from app.main import app  # noqa: E402

OS6 = {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "10.0.0.31", "platform": "dell_os6", "enabled": True}
OS10 = {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "10.0.0.10", "platform": "dell_os10", "enabled": True}


class FakeLocks:
    """In-memory stand-in for the SET NX EX / compare-and-delete helpers in app.cache."""

    def __init__(self):
        self.store = {}

    def claim(self, key, ttl, value="1"):
        if key in self.store:
            return False
        self.store[key] = value
        return True

    def get_value(self, key):
        return self.store.get(key)

    def release(self, key, token):
        if self.store.get(key) == token:
            del self.store[key]


class BackupRequestTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.client = TestClient(app)
        self.devices = {1: OS6, 12: OS10}
        mock.patch.object(device_backup, "get_device_by_id", side_effect=lambda i: self.devices.get(i)).start()
        self.locks = FakeLocks()
        for name in ("claim", "get_value", "release"):
            mock.patch.object(device_backup.cache, name, side_effect=getattr(self.locks, name)).start()
        self.create_job = mock.patch.object(device_backup, "create_queued_job").start()
        self.mark_failed = mock.patch.object(device_backup, "mark_job_failed").start()
        self.audit = mock.patch.object(device_backup, "create_audit_event").start()
        self.send = mock.patch.object(device_backup.celery_app, "send_task").start()
        self.send.return_value.id = "celery-task-id"

    def test_valid_request_returns_202_and_queues_manual_task(self):
        response = self.client.post("/api/v1/devices/1/backup")

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["device_id"], 1)
        self.assertEqual(body["message"], "Backup queued for Kenda-HARO-IDF-A")
        job_id = body["job_id"]
        uuid.UUID(job_id)

        self.create_job.assert_called_once_with(job_id, 1, "manual_backup", "dashboard")
        name = self.send.call_args.args[0]
        self.assertEqual(name, "network_worker.manual_backup_device")
        self.assertEqual(self.send.call_args.kwargs["kwargs"], {"device_id": 1, "job_id": job_id, "lock_token": job_id})
        self.assertEqual(self.audit.call_args.kwargs["event_type"], "backup_requested")
        self.assertIn("source=manual", self.audit.call_args.kwargs["message"])
        self.assertEqual(self.locks.store, {"backup:device:1:lock": job_id})

    def test_os10_is_supported(self):
        self.assertEqual(self.client.post("/api/v1/devices/12/backup").status_code, 202)

    def test_nonexistent_device_404(self):
        self.assertEqual(self.client.post("/api/v1/devices/999/backup").status_code, 404)
        self.send.assert_not_called()

    def test_unsupported_platform_422(self):
        self.devices[5] = {**OS6, "id": 5, "platform": "cisco_ios"}
        response = self.client.post("/api/v1/devices/5/backup")
        self.assertEqual(response.status_code, 422)
        self.assertIn("cisco_ios", response.json()["detail"])
        self.send.assert_not_called()
        self.create_job.assert_not_called()

    def test_disabled_device_422(self):
        self.devices[6] = {**OS6, "id": 6, "enabled": False}
        self.assertEqual(self.client.post("/api/v1/devices/6/backup").status_code, 422)
        self.send.assert_not_called()

    def test_duplicate_while_running_409_reports_running_job(self):
        first = self.client.post("/api/v1/devices/1/backup").json()["job_id"]
        second = self.client.post("/api/v1/devices/1/backup")

        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], "Backup already running for this device.")
        self.assertEqual(second.json()["job_id"], first)
        self.assertEqual(self.send.call_count, 1)

    def test_other_devices_are_independent(self):
        self.assertEqual(self.client.post("/api/v1/devices/1/backup").status_code, 202)
        self.assertEqual(self.client.post("/api/v1/devices/12/backup").status_code, 202)

    def test_queue_unavailable_503_fails_job_and_frees_lock(self):
        self.send.side_effect = ConnectionError("redis down")
        response = self.client.post("/api/v1/devices/1/backup")

        self.assertEqual(response.status_code, 503)
        self.mark_failed.assert_called_once()
        self.assertEqual(self.mark_failed.call_args.args[1], "Task queue unavailable")
        self.assertEqual(self.locks.store, {})

    def test_job_insert_failure_frees_lock(self):
        self.create_job.side_effect = RuntimeError("db")
        self.assertEqual(self.client.post("/api/v1/devices/1/backup").status_code, 500)
        self.assertEqual(self.locks.store, {})
        self.send.assert_not_called()

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get("/api/v1/devices/1/backup").status_code, 405)


class JobStatusTests(unittest.TestCase):
    JOB = "22222222-2222-2222-2222-222222222222"

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.client = TestClient(app)
        self.get_job = mock.patch.object(device_backup, "get_job").start()
        mock.patch.object(device_backup, "file_available", return_value=True).start()

    def row(self, **overrides):
        return {
            "id": self.JOB, "device_id": 1, "hostname": "Kenda-HARO-IDF-A", "job_type": "manual_backup",
            "status": "success", "requested_by": "dashboard", "started_at": "s", "finished_at": "f",
            "error_message": None, "backup_id": 42,
            "backup_storage_path": "/backups/Kenda-HARO-IDF-A/20260930T120000Z.cfg",
            "backup_created_at": "2026-09-30T12:00:00+00:00", **overrides,
        }

    def test_success_exposes_backup_identity_not_path(self):
        self.get_job.return_value = self.row()
        body = self.client.get(f"/api/v1/jobs/{self.JOB}").json()

        self.assertEqual(body["status"], "success")
        self.assertEqual(body["backup"], {
            "backup_id": 42, "device_id": 1, "filename": "Kenda-HARO-IDF-A_20260930T120000Z.cfg",
            "created_at": "2026-09-30T12:00:00+00:00", "file_available": True})
        self.assertNotIn("/backups", str(body))

    def test_failed_job_has_error_and_no_backup(self):
        self.get_job.return_value = self.row(status="failed", error_message="Device unreachable",
                                             backup_id=None, backup_storage_path=None, backup_created_at=None)
        body = self.client.get(f"/api/v1/jobs/{self.JOB}").json()
        self.assertEqual(body["error_message"], "Device unreachable")
        self.assertIsNone(body["backup"])

    def test_unknown_and_invalid_job(self):
        self.get_job.return_value = None
        self.assertEqual(self.client.get(f"/api/v1/jobs/{self.JOB}").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/jobs/not-a-uuid").status_code, 400)


class DownloadTests(unittest.TestCase):
    """GET /api/v1/backups/{id}/download: path comes only from the DB row, confined to BACKUP_ROOT."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(os.path.join(self.tmp.name, "backups"))
        os.makedirs(os.path.join(self.root, "Kenda-HARO-IDF-A"))
        self.good = os.path.join(self.root, "Kenda-HARO-IDF-A", "20260930T120000Z.cfg")
        with open(self.good, "w") as f:
            f.write("hostname Kenda-HARO-IDF-A\n")
        self.outside = os.path.join(self.tmp.name, "secret.txt")
        with open(self.outside, "w") as f:
            f.write("not a backup\n")
        mock.patch.object(backup_download, "BACKUP_ROOT", self.root).start()
        self.rows = {}
        mock.patch.object(backup_download, "get_backup_by_id", side_effect=lambda i: self.rows.get(i)).start()
        self.audit = mock.patch.object(backup_download, "create_audit_event").start()
        self.client = TestClient(app)

    def add(self, backup_id, path):
        self.rows[backup_id] = {"id": backup_id, "device_id": 1, "storage_path": path, "hostname": "Kenda-HARO-IDF-A"}

    def test_valid_backup_is_attachment(self):
        self.add(1, self.good)
        response = self.client.get("/api/v1/backups/1/download")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "hostname Kenda-HARO-IDF-A\n")
        self.assertIn('attachment; filename="Kenda-HARO-IDF-A_20260930T120000Z.cfg"', response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.audit.call_args.kwargs["event_type"], "backup_downloaded")

    def test_unknown_backup_404(self):
        self.assertEqual(self.client.get("/api/v1/backups/999/download").status_code, 404)

    def test_record_whose_file_is_missing_404(self):
        self.add(2, os.path.join(self.root, "Kenda-HARO-IDF-A", "gone.cfg"))
        self.assertEqual(self.client.get("/api/v1/backups/2/download").status_code, 404)

    def test_traversal_in_stored_path_refused(self):
        self.add(3, os.path.join(self.root, "..", "secret.txt"))
        self.add(4, "/etc/passwd")
        self.add(5, self.root)
        for backup_id in (3, 4, 5):
            with self.subTest(backup_id=backup_id):
                self.assertEqual(self.client.get(f"/api/v1/backups/{backup_id}/download").status_code, 404)

    def test_symlink_escaping_root_refused(self):
        link = os.path.join(self.root, "Kenda-HARO-IDF-A", "link.cfg")
        os.symlink(self.outside, link)
        self.add(6, link)
        self.assertEqual(self.client.get("/api/v1/backups/6/download").status_code, 404)

    def test_client_supplied_paths_are_ignored_or_rejected(self):
        self.add(1, self.good)
        # A query parameter is never read: the file is still the DB row's.
        response = self.client.get("/api/v1/backups/1/download", params={"path": "/etc/passwd"})
        self.assertEqual(response.text, "hostname Kenda-HARO-IDF-A\n")
        for bad in ("..%2F..%2Fetc%2Fpasswd", "1..", "%2Fetc%2Fpasswd", "-1abc"):
            with self.subTest(bad=bad):
                self.assertIn(self.client.get(f"/api/v1/backups/{bad}/download").status_code, (404, 422))

    def test_list_marks_missing_files_unavailable(self):
        rows = [
            {"id": 1, "storage_path": self.good},
            {"id": 2, "storage_path": os.path.join(self.root, "x", "gone.cfg")},
            {"id": 3, "storage_path": self.outside},
        ]
        with mock.patch("app.main.list_backups", return_value=[dict(r) for r in rows]):
            body = self.client.get("/api/v1/backups").json()
        self.assertEqual([b["file_available"] for b in body], [True, False, False])


if __name__ == "__main__":
    unittest.main()
