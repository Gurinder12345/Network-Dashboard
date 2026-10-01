"""
End-to-end PCAP analysis against a REAL, disposable PostgreSQL and the real TShark:
API upload -> worker task (analyze + cleanup) -> API status/result -> delete.
Celery is bypassed (the queued task is run directly); Redis is not needed.
Skipped unless PCAP_TEST_PG_DSN is set. Never point it at the production database.

Run inside the worker image (tshark) with fastapi/httpx/python-multipart installed:
    PCAP_TEST_PG_DSN=postgresql://networkapp:x@127.0.0.1:55441/networkdb \
    python -m unittest tests.test_pcap_flow_pg -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

DSN = os.getenv("PCAP_TEST_PG_DSN")
HERE = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.dirname(HERE)
WORKER_DIR = os.path.join(os.path.dirname(API_DIR), "network-worker")
REPO = os.path.dirname(os.path.dirname(API_DIR))
FIX = os.path.join(WORKER_DIR, "tests", "fixtures", "pcap")


@unittest.skipUnless(DSN, "PCAP_TEST_PG_DSN not set (needs a disposable PostgreSQL)")
class PcapFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path[:0] = [API_DIR, WORKER_DIR]
        import psycopg
        from fastapi.testclient import TestClient

        from app import pcap_analysis as api
        from app.db import client as api_db
        from app.main import app
        from db import client as worker_db
        from pcap import task

        url = urlparse(DSN)
        for module in (api_db, worker_db):
            module.DB_HOST, module.DB_PORT, module.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
            module.DB_USER, module.DB_PASSWORD = url.username, url.password
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS pcap_analyses, audit_events, jobs, devices CASCADE")
            conn.execute("CREATE TABLE devices (id bigserial PRIMARY KEY, hostname varchar)")
            conn.execute("CREATE TABLE jobs (id uuid PRIMARY KEY)")
            conn.execute("CREATE TABLE audit_events (id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id), "
                         "device_id bigint REFERENCES devices(id), event_type varchar NOT NULL, message text, "
                         "created_at timestamptz NOT NULL DEFAULT now())")
            migration = open(os.path.join(REPO, "db", "migrations", "006_pcap_analyses.sql")).read()
            conn.execute(migration)
            conn.execute(migration)  # re-run must be safe
        cls.psycopg, cls.api, cls.task, cls.client = psycopg, api, task, TestClient(app)

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        mock.patch.object(self.api, "PCAP_ROOT", self.root).start()
        mock.patch.object(self.task, "PCAP_ROOT", self.root).start()
        self.queued = []
        send = mock.patch.object(self.api.celery_app, "send_task").start()
        send.side_effect = lambda name, kwargs, **_: self.queued.append(kwargs["analysis_id"])

    def upload(self, files, mode):
        handles = {field: (name, open(os.path.join(FIX, name), "rb")) for field, name in files.items()}
        try:
            return self.client.post("/api/v1/pcap-analysis", data={"mode": mode}, files=handles)
        finally:
            for _, handle in handles.values():
                handle.close()

    def query(self, sql, *args):
        with self.psycopg.connect(DSN) as conn:
            return conn.execute(sql, args).fetchall()

    def test_single_capture_end_to_end(self):
        r = self.upload({"client_file": "tcp_zero_window.pcap"}, "single")
        self.assertEqual(r.status_code, 202, r.text)
        analysis_id = r.json()["analysis_id"]
        self.assertEqual(self.client.get(f"/api/v1/pcap-analysis/{analysis_id}").json()["status"], "queued")

        self.task.run_analysis(self.queued[-1])

        body = self.client.get(f"/api/v1/pcap-analysis/{analysis_id}").json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual((body["packet_count"], body["flow_count"], body["client_filename"]), (14, 1, "tcp_zero_window.pcap"))
        self.assertEqual(body["likely_issue"], "Receiver could not keep up (TCP zero window)")
        self.assertEqual(body["finding_counts"], {"critical": 0, "warning": 1, "info": 0})
        self.assertEqual(body["result"]["findings"][0]["cause"], "receiver_window")
        self.assertIsNotNone(body["files_deleted_at"])
        self.assertFalse(os.path.exists(os.path.join(self.root, analysis_id)))
        self.assertNotIn("client_file", body)          # internal names stay server-side
        self.assertNotIn(self.root, str(body))

        listed = self.client.get("/api/v1/pcap-analysis").json()
        self.assertEqual(listed[0]["id"], analysis_id)
        self.assertNotIn("result", listed[0])

        events = [r[0] for r in self.query("SELECT event_type FROM audit_events ORDER BY id")]
        self.assertEqual(events[-2:], ["pcap_analysis_requested", "pcap_analysis_completed"])

        self.assertEqual(self.client.delete(f"/api/v1/pcap-analysis/{analysis_id}").json()["status"], "deleted")
        self.assertEqual(self.client.get(f"/api/v1/pcap-analysis/{analysis_id}").status_code, 404)

    def test_dual_capture_end_to_end(self):
        r = self.upload({"client_file": "dual_delay_client.pcap", "server_file": "dual_delay_server.pcap"}, "dual")
        analysis_id = r.json()["analysis_id"]
        self.task.run_analysis(self.queued[-1])
        result = self.client.get(f"/api/v1/pcap-analysis/{analysis_id}").json()["result"]
        self.assertEqual(result["mode"], "dual")
        self.assertEqual(result["correlation"]["matched_flows"], 1)
        self.assertEqual(result["correlation"]["clock"]["confidence"], "high")
        self.assertEqual(result["assessment"]["cause"], "app_delay")

    def test_malformed_capture_fails_cleanly(self):
        r = self.upload({"client_file": "truncated.pcap"}, "single")
        self.assertEqual(r.status_code, 202)  # valid magic; content checked by the worker
        analysis_id = r.json()["analysis_id"]
        self.task.run_analysis(self.queued[-1])
        body = self.client.get(f"/api/v1/pcap-analysis/{analysis_id}").json()
        self.assertEqual(body["status"], "failed")
        self.assertRegex(body["error"], "cut short|could not be read|no packets")
        self.assertNotIn(self.root, body["error"])
        self.assertFalse(os.path.exists(os.path.join(self.root, analysis_id)))

    def test_active_analysis_cannot_be_deleted_and_stale_ones_are_failed(self):
        r = self.upload({"client_file": "tcp_normal.pcap"}, "single")
        analysis_id = r.json()["analysis_id"]
        self.assertEqual(self.client.delete(f"/api/v1/pcap-analysis/{analysis_id}").status_code, 409)
        self.query("UPDATE pcap_analyses SET updated_at = now() - interval '2 hours' WHERE id = %s RETURNING id", analysis_id)
        out = self.task.cleanup()
        self.assertIn(analysis_id, out["stale_failed"])
        self.assertIn(analysis_id, out["removed_dirs"])
        self.assertEqual(self.client.get(f"/api/v1/pcap-analysis/{analysis_id}").json()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
