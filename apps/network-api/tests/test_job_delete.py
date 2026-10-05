"""
Manual job deletion (soft delete). Route behaviour with a mocked DB, and the real SQL /
transaction against a disposable PostgreSQL when JOBS_TEST_PG_DSN is set. Run from
apps/network-api:
    python -m unittest tests.test_job_delete -v
"""

import os
import sys
import threading
import time
import unittest
import uuid
from unittest import mock
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import job_actions  # noqa: E402
from app.main import app  # noqa: E402

JOB = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.delete = mock.patch.object(job_actions, "delete_job").start()
        self.client = TestClient(app)

    def test_deleted_is_204_without_body(self):
        self.delete.return_value = {"outcome": "deleted", "job_type": "manual_backup", "previous_status": "failed",
                                    "device_id": 1, "deleted_at": "t"}
        r = self.client.delete(f"/api/v1/jobs/{JOB}")
        self.assertEqual((r.status_code, r.content), (204, b""))
        self.delete.assert_called_once_with(JOB, "dashboard")

    def test_conflicts_and_not_found(self):
        self.delete.return_value = {"outcome": "active", "status": "running"}
        r = self.client.delete(f"/api/v1/jobs/{JOB}")
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "Job cannot be deleted while status is running."))
        self.delete.return_value = {"outcome": "in_use", "approval_id": "a1", "approval_status": "approved"}
        r = self.client.delete(f"/api/v1/jobs/{JOB}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("approval a1, which is still approved", r.json()["detail"])
        self.delete.return_value = {"outcome": "not_found"}
        self.assertEqual(self.client.delete(f"/api/v1/jobs/{JOB}").status_code, 404)

    def test_invalid_id_and_db_error(self):
        self.assertEqual(self.client.delete("/api/v1/jobs/not-a-uuid").status_code, 400)
        self.delete.assert_not_called()
        self.delete.side_effect = RuntimeError("password=secret in DSN")
        r = self.client.delete(f"/api/v1/jobs/{JOB}")
        self.assertEqual((r.status_code, r.json()["detail"]), (500, "Unable to delete job"))
        self.assertNotIn("secret", r.text)


DSN = os.getenv("JOBS_TEST_PG_DSN")

# Worst case on purpose: every FK into jobs CASCADEs. A hard DELETE would wipe backups,
# approvals and audit history; the soft delete must leave all of them in place.
SCHEMA = """
DROP TABLE IF EXISTS audit_events, change_approvals, backups, jobs, devices CASCADE;
CREATE TABLE devices (id bigserial PRIMARY KEY, hostname varchar NOT NULL);
CREATE TABLE jobs (id uuid PRIMARY KEY, device_id bigint REFERENCES devices(id) ON DELETE CASCADE, job_type varchar,
    status varchar, requested_by varchar, started_at timestamptz, finished_at timestamptz, error_message text);
CREATE TABLE backups (id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id) ON DELETE CASCADE, device_id bigint,
    backup_type varchar, storage_path text, checksum varchar, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE change_approvals (id uuid PRIMARY KEY, device_id bigint NOT NULL, backup_job_id uuid NOT NULL
    REFERENCES jobs(id) ON DELETE CASCADE, requested_by varchar NOT NULL, approved_by varchar,
    status varchar NOT NULL DEFAULT 'pending', config_lines jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE audit_events (id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id) ON DELETE CASCADE,
    device_id bigint REFERENCES devices(id), event_type varchar NOT NULL, message text,
    created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO devices VALUES (1, 'Kenda-HARO-IDF-A');
"""


@unittest.skipUnless(DSN, "JOBS_TEST_PG_DSN not set (needs a disposable PostgreSQL)")
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        from app.db import client, jobs

        url = urlparse(DSN)
        client.DB_HOST, client.DB_PORT, client.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
        client.DB_USER, client.DB_PASSWORD = url.username, url.password
        cls.psycopg, cls.jobs = psycopg, jobs
        cls.migration = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "db", "migrations",
                                          "007_jobs_soft_delete.sql")).read()

    def setUp(self):
        with self.psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(SCHEMA)
            conn.execute(self.migration)
            conn.execute(self.migration)  # re-run must be safe
        self.client = TestClient(app)

    def sql(self, statement, *args):
        with self.psycopg.connect(DSN, autocommit=True) as conn:
            cur = conn.execute(statement, args)
            return cur.fetchall() if cur.description else None

    def job(self, status, job_type="manual_backup", with_backup=True, approval_status=None):
        job_id = str(uuid.uuid4())
        self.sql("INSERT INTO jobs VALUES (%s, 1, %s, %s, 'dashboard', now(), now(), NULL)", job_id, job_type, status)
        self.sql("INSERT INTO audit_events (job_id, device_id, event_type, message) VALUES (%s, 1, 'backup_started', 'x')", job_id)
        if with_backup:
            self.sql("INSERT INTO backups (job_id, device_id, backup_type, storage_path, checksum) "
                     "VALUES (%s, 1, 'running-config', '/backups/x.cfg', 'abc')", job_id)
        if approval_status:
            self.sql("INSERT INTO change_approvals (id, device_id, backup_job_id, requested_by, status, config_lines) "
                     "VALUES (%s, 1, %s, 'system', %s, '[\"vlan 200\"]')", str(uuid.uuid4()), job_id, approval_status)
        return job_id

    def counts(self, job_id):
        return {
            "job_rows": self.sql("SELECT count(*) FROM jobs WHERE id = %s", job_id)[0][0],
            "backups": self.sql("SELECT count(*) FROM backups WHERE job_id = %s", job_id)[0][0],
            "approvals": self.sql("SELECT count(*) FROM change_approvals WHERE backup_job_id = %s", job_id)[0][0],
            "audit_history": self.sql("SELECT count(*) FROM audit_events WHERE job_id = %s AND event_type <> 'job_deleted'", job_id)[0][0],
        }

    def listed(self):
        return {j["id"] for j in self.client.get("/api/v1/jobs").json()}

    def test_finished_jobs_are_deleted_and_nothing_related_is_lost(self):
        for status in ("success", "failed", "completed", "cancelled"):
            with self.subTest(status=status):
                job_id = self.job(status, approval_status="applied" if status == "success" else None)
                before = self.counts(job_id)
                self.assertIn(job_id, self.listed())

                r = self.client.delete(f"/api/v1/jobs/{job_id}")

                self.assertEqual(r.status_code, 204)
                self.assertNotIn(job_id, self.listed())
                self.assertEqual(self.client.get(f"/api/v1/jobs/{job_id}").status_code, 404)
                self.assertEqual(self.counts(job_id), before)  # row, backups, approvals, audit history all kept
                event = self.sql("SELECT device_id, message FROM audit_events WHERE job_id = %s AND event_type = 'job_deleted'", job_id)
                self.assertEqual(len(event), 1)
                self.assertEqual(event[0][0], 1)
                for part in (job_id, "job_type=manual_backup", f"previous_status={status}", "device_id=1", "deleted_at=", "deleted_by=dashboard"):
                    self.assertIn(part, event[0][1])
                self.assertEqual(self.sql("SELECT deleted_by FROM jobs WHERE id = %s", job_id)[0][0], "dashboard")

    def test_backup_still_listed_with_its_source(self):
        job_id = self.job("failed")
        self.client.delete(f"/api/v1/jobs/{job_id}")
        backups = [b for b in self.client.get("/api/v1/backups").json() if b["job_id"] == job_id]
        self.assertEqual(backups[0]["job_type"], "manual_backup")

    def test_active_and_unknown_statuses_are_refused(self):
        for status in ("queued", "running", "pending", "applying", "approved", "something_new"):
            with self.subTest(status=status):
                job_id = self.job(status, with_backup=False)
                r = self.client.delete(f"/api/v1/jobs/{job_id}")
                self.assertEqual(r.status_code, 409)
                self.assertEqual(r.json()["detail"], f"Job cannot be deleted while status is {status}.")
                self.assertIn(job_id, self.listed())
                self.assertEqual(self.sql("SELECT count(*) FROM audit_events WHERE event_type = 'job_deleted' AND job_id = %s", job_id)[0][0], 0)

    def test_backup_of_an_in_flight_approval_is_refused(self):
        for approval in ("pending", "approved", "applying"):
            with self.subTest(approval=approval):
                job_id = self.job("success", job_type="config_backup", approval_status=approval)
                r = self.client.delete(f"/api/v1/jobs/{job_id}")
                self.assertEqual(r.status_code, 409)
                self.assertIn(f"which is still {approval}", r.json()["detail"])
                self.assertIn(job_id, self.listed())

    def test_missing_and_already_deleted(self):
        self.assertEqual(self.client.delete(f"/api/v1/jobs/{uuid.uuid4()}").status_code, 404)
        job_id = self.job("failed")
        self.assertEqual(self.client.delete(f"/api/v1/jobs/{job_id}").status_code, 204)
        self.assertEqual(self.client.delete(f"/api/v1/jobs/{job_id}").status_code, 404)
        self.assertEqual(self.sql("SELECT count(*) FROM audit_events WHERE event_type = 'job_deleted' AND job_id = %s", job_id)[0][0], 1)

    def test_status_change_after_the_ui_read_is_respected(self):
        job_id = self.job("failed", with_backup=False)  # the UI saw "failed"
        self.sql("UPDATE jobs SET status = 'running' WHERE id = %s", job_id)  # changed before DELETE arrives
        self.assertEqual(self.client.delete(f"/api/v1/jobs/{job_id}").status_code, 409)

    def test_concurrent_uncommitted_status_change_is_waited_for(self):
        job_id = self.job("failed", with_backup=False)
        other = self.psycopg.connect(DSN)
        other.execute("UPDATE jobs SET status = 'running' WHERE id = %s", (job_id,))  # row locked, not committed
        result = {}
        thread = threading.Thread(target=lambda: result.update(status=self.client.delete(f"/api/v1/jobs/{job_id}").status_code))
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive())  # DELETE is blocked on FOR UPDATE, not reading stale state
        other.commit()
        other.close()
        thread.join(10)
        self.assertEqual(result["status"], 409)
        self.assertIn(job_id, self.listed())


if __name__ == "__main__":
    unittest.main()
