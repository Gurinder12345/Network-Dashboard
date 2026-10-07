"""
End-to-end manual backup flow against a REAL, disposable PostgreSQL: API trigger -> worker
task body -> job/backup/audit rows -> job status -> download, plus the change-workflow
backup path. The device collector is stubbed (no switch, no Vault); Celery and Redis are
faked. Skipped unless BACKUP_TEST_PG_DSN is set. Never point this at the production DB.

Needs the worker's and the API's dependencies (e.g. the worker image + fastapi/httpx):
    BACKUP_TEST_PG_DSN=postgresql://networkapp:x@127.0.0.1:55436/networkdb \
    python -m unittest tests.test_backup_flow_pg -v
"""

import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

DSN = os.getenv("BACKUP_TEST_PG_DSN")

HERE = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.dirname(HERE)
WORKER_DIR = os.path.join(os.path.dirname(API_DIR), "network-worker")

CONFIG = "! Kenda-HARO-IDF-A running-config (test)\nhostname Kenda-HARO-IDF-A\n!\nend\n"

SCHEMA = """
DROP TABLE IF EXISTS audit_events, change_approvals, backups, jobs, devices CASCADE;
CREATE TABLE devices (id bigserial PRIMARY KEY, hostname varchar NOT NULL, management_ip inet NOT NULL,
    platform varchar NOT NULL, enabled boolean NOT NULL DEFAULT true, credential_path varchar);
CREATE TABLE jobs (id uuid PRIMARY KEY, device_id bigint REFERENCES devices(id), job_type varchar,
    status varchar, requested_by varchar, started_at timestamptz, finished_at timestamptz, error_message text);
CREATE TABLE backups (id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id), device_id bigint REFERENCES devices(id),
    backup_type varchar, storage_path text, checksum varchar, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE audit_events (id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id), device_id bigint REFERENCES devices(id),
    event_type varchar NOT NULL, message text, created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO devices (id, hostname, management_ip, platform, enabled, credential_path) VALUES
    (1, 'Kenda-HARO-IDF-A', '10.0.0.31', 'dell_os6', true, 'x'),
    (12, 'Kenda-Core-1', '10.0.0.10', 'dell_os10', true, 'x');
"""


@unittest.skipUnless(DSN, "BACKUP_TEST_PG_DSN not set (needs a disposable PostgreSQL)")
class ManualBackupFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = urlparse(DSN)
        os.environ.update(DB_HOST=url.hostname, DB_PORT=str(url.port or 5432), DB_NAME=url.path.lstrip("/"),
                          DB_USER=url.username, DB_PASSWORD=url.password)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.realpath(cls.tmp.name)
        os.environ["BACKUP_ROOT"] = cls.root
        sys.path[:0] = [API_DIR, WORKER_DIR, os.path.join(WORKER_DIR, "tests")]

        import psycopg
        from fastapi.testclient import TestClient

        from app import device_backup
        from app.main import app
        from tasks import config_backup
        import worker

        cls.psycopg, cls.device_backup, cls.config_backup, cls.worker = psycopg, device_backup, config_backup, worker
        cls.client = TestClient(app)

        # Modules may already have been imported by other test modules (reading the env at
        # import time), so set the effective settings on the modules themselves.
        from app import backup_download
        from app.db import client as api_db
        from db import client as worker_db

        for module in (api_db, worker_db):
            module.DB_HOST, module.DB_PORT, module.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
            module.DB_USER, module.DB_PASSWORD = url.username, url.password
        config_backup.BACKUP_ROOT = cls.root
        backup_download.BACKUP_ROOT = cls.root

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with self.psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(SCHEMA)
            # Production schema after the job-deletion release (jobs.deleted_at / deleted_by).
            with open(os.path.join(os.path.dirname(API_DIR), "..", "db", "migrations", "007_jobs_soft_delete.sql")) as handle:
                conn.execute(handle.read())
        self.addCleanup(mock.patch.stopall)
        from fakes import fake_redis

        fake_redis.install(self)  # in-memory device coordination (the backup takes the device slot)
        locks = {}
        cache = self.device_backup.cache
        mock.patch.object(cache, "claim", side_effect=lambda k, t, value="1": False if k in locks else not locks.update({k: value})).start()
        mock.patch.object(cache, "get_value", side_effect=locks.get).start()
        mock.patch.object(cache, "release", side_effect=lambda k, v: locks.pop(k, None) if locks.get(k) == v else None).start()
        self.sent = []
        send = mock.patch.object(self.device_backup.celery_app, "send_task").start()
        send.side_effect = lambda name, kwargs, **_: self.sent.append(kwargs) or mock.Mock(id="task-1")
        self.collector = mock.Mock(side_effect=lambda host: {host: {
            "failed": False, "timestamp": "20260930T154200Z",
            "checksum": hashlib.sha256(CONFIG.encode()).hexdigest(), "config": CONFIG}})
        mock.patch.dict(self.config_backup.COLLECTORS, {"dell_os6": self.collector, "dell_os10": self.collector}).start()
        mock.patch.object(self.worker, "release_device_backup_lock").start()

    def query(self, sql, *args):
        with self.psycopg.connect(DSN) as conn:
            return conn.execute(sql, args).fetchall()

    def run_worker(self):
        kwargs = self.sent[-1]
        return self.worker.manual_backup_device(**kwargs)

    def test_backup_now_to_download(self):
        response = self.client.post("/api/v1/devices/1/backup")
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job_id"]

        self.assertEqual(self.query("SELECT status, job_type, requested_by FROM jobs WHERE id = %s", job_id),
                         [("queued", "manual_backup", "dashboard")])
        self.assertEqual(self.client.get(f"/api/v1/jobs/{job_id}").json()["status"], "queued")

        result = self.run_worker()
        self.assertEqual(result["status"], "success")
        self.collector.assert_called_once_with("Kenda-HARO-IDF-A")

        job = self.client.get(f"/api/v1/jobs/{job_id}").json()
        self.assertEqual(job["status"], "success")
        self.assertIsNotNone(job["finished_at"])
        backup = job["backup"]
        self.assertEqual(backup["backup_id"], result["backup_id"])
        self.assertEqual(backup["filename"], "Kenda-HARO-IDF-A_20260930T154200Z.cfg")
        self.assertTrue(backup["file_available"])

        events = [r[0] for r in self.query("SELECT event_type FROM audit_events WHERE job_id = %s ORDER BY id", job_id)]
        self.assertEqual(events, ["backup_requested", "backup_started", "backup_completed"])
        self.assertFalse(any(CONFIG.strip() in (r[0] or "") for r in self.query("SELECT message FROM audit_events")))

        download = self.client.get(f"/api/v1/backups/{backup['backup_id']}/download")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.text, CONFIG)
        self.assertIn(backup["filename"], download.headers["content-disposition"])

        listed = [b for b in self.client.get("/api/v1/backups").json() if b["id"] == backup["backup_id"]][0]
        self.assertEqual((listed["job_type"], listed["hostname"], listed["file_available"]),
                         ("manual_backup", "Kenda-HARO-IDF-A", True))
        jobs = [j for j in self.client.get("/api/v1/jobs").json() if j["id"] == job_id]
        self.assertEqual(jobs[0]["backup_id"], backup["backup_id"])

    def test_failure_records_failed_job_without_backup(self):
        self.collector.side_effect = lambda host: {host: {"failed": True, "result": "Authentication to device failed."}}
        job_id = self.client.post("/api/v1/devices/1/backup").json()["job_id"]

        self.assertEqual(self.run_worker()["status"], "failed")

        job = self.client.get(f"/api/v1/jobs/{job_id}").json()
        self.assertEqual((job["status"], job["error_message"], job["backup"]), ("failed", "SSH authentication failed", None))
        self.assertEqual(self.query("SELECT count(*) FROM backups")[0][0], 0)
        events = [r[0] for r in self.query("SELECT event_type FROM audit_events WHERE job_id = %s ORDER BY id", job_id)]
        self.assertEqual(events, ["backup_requested", "backup_started", "backup_failed"])

    def test_redelivered_task_does_not_back_up_twice(self):
        self.client.post("/api/v1/devices/12/backup")
        self.assertEqual(self.run_worker()["status"], "success")
        self.assertEqual(self.run_worker()["status"], "skipped")
        self.assertEqual(self.collector.call_count, 1)
        self.assertEqual(self.query("SELECT count(*) FROM backups")[0][0], 1)

    def test_change_workflow_backup_still_valid_for_approval(self):
        from db.approvals import verify_backup_for_device

        result = self.worker.backup_running_config_task("Kenda-HARO-IDF-A", config_snapshot=CONFIG)

        self.collector.assert_not_called()  # precheck snapshot reused, no second SSH session
        self.assertEqual(set(result), {"job_id", "status", "hostname", "platform", "storage_path", "checksum"})
        self.assertEqual(self.query("SELECT job_type, status, requested_by FROM jobs WHERE id = %s", result["job_id"]),
                         [("config_backup", "success", "celery")])
        check = verify_backup_for_device(1, result["job_id"])
        self.assertTrue(check["valid"])
        self.assertEqual(check["checksum"], hashlib.sha256(CONFIG.encode()).hexdigest())
        messages = [r[0] for r in self.query("SELECT message FROM audit_events WHERE job_id = %s ORDER BY id", result["job_id"])]
        self.assertEqual(messages[0], "Running-config backup started for Kenda-HARO-IDF-A")
        self.assertTrue(messages[1].startswith("Backup completed: ") and "source=" not in messages[1])


if __name__ == "__main__":
    unittest.main()
