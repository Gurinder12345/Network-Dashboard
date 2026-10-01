"""
PCAP Analyzer API: streaming upload validation, storage safety, status/list/delete.
DB and Celery are mocked; files go to a temporary PCAP_ROOT. Needs fastapi, httpx and
python-multipart. Run from apps/network-api:
    python -m unittest tests.test_pcap_api -v
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import pcap_analysis as api  # noqa: E402
from app.main import app  # noqa: E402

PCAP = b"\xd4\xc3\xb2\xa1" + b"\x02\x00\x04\x00" + b"\0" * 16 + b"rest-of-capture"
PCAPNG = b"\x0a\x0d\x0d\x0a" + b"\x1c\x00\x00\x00" + b"\0" * 24


class PcapApiTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        mock.patch.object(api, "PCAP_ROOT", self.root).start()
        self.db = {name: mock.patch.object(api.db, name).start() for name in
                   ("count_active", "insert_analysis", "mark_failed", "list_recent", "get_analysis", "delete_analysis")}
        self.db["count_active"].return_value = 0
        self.send = mock.patch.object(api.celery_app, "send_task").start()
        self.audit = mock.patch.object(api, "create_audit_event").start()
        self.client = TestClient(app)

    def post(self, files, mode="single", extra=None):
        data = {"mode": mode} if mode is not None else {}
        data.update(extra or {})
        return self.client.post("/api/v1/pcap-analysis", data=data, files=files)

    def dirs(self):
        return os.listdir(self.root)


class UploadTests(PcapApiTestCase):
    def test_valid_pcap_single(self):
        r = self.post({"client_file": ("trace.pcap", PCAP, "application/vnd.tcpdump.pcap")})
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertEqual((body["status"], body["mode"]), ("queued", "single"))
        analysis_id = body["analysis_id"]
        uuid.UUID(analysis_id)
        directory = os.path.join(self.root, analysis_id)
        self.assertEqual(os.listdir(directory), ["client.pcap"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, "client.pcap")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        with open(os.path.join(directory, "client.pcap"), "rb") as handle:
            self.assertEqual(handle.read(), PCAP)
        args = self.db["insert_analysis"].call_args.args
        self.assertEqual((args[0], args[1], args[2]["client"]["stored_name"], args[2]["client"]["display_name"], args[2]["client"]["size"]),
                         (analysis_id, "single", "client.pcap", "trace.pcap", len(PCAP)))
        self.assertEqual(self.send.call_args.args[0], "network_worker.analyze_pcap")
        self.assertEqual(self.send.call_args.kwargs["kwargs"], {"analysis_id": analysis_id})
        self.assertEqual(self.audit.call_args.kwargs["event_type"], "pcap_analysis_requested")
        self.assertNotIn(self.root, r.text)

    def test_valid_pcapng_and_dual(self):
        r = self.post({"client_file": ("c.pcapng", PCAPNG), "server_file": ("s.pcap", PCAP)}, mode="dual")
        self.assertEqual(r.status_code, 202, r.text)
        directory = os.path.join(self.root, r.json()["analysis_id"])
        self.assertEqual(sorted(os.listdir(directory)), ["client.pcapng", "server.pcap"])

    def test_extension_and_magic_are_both_required(self):
        r = self.post({"client_file": ("notes.txt", PCAP)})
        self.assertEqual(r.status_code, 415)
        r = self.post({"client_file": ("fake.pcap", b"MZ\x90\x00 definitely not a capture")})
        self.assertEqual(r.status_code, 415)
        self.assertIn("not a pcap or pcapng", r.json()["detail"])
        self.assertEqual(self.dirs(), [])  # rejected uploads leave nothing behind
        self.send.assert_not_called()

    def test_empty_file(self):
        self.assertEqual(self.post({"client_file": ("e.pcap", b"")}).status_code, 422)
        self.assertEqual(self.dirs(), [])

    def test_oversized_file_rejected_while_streaming(self):
        with mock.patch.object(api, "MAX_FILE_BYTES", 1024):
            r = self.post({"client_file": ("big.pcap", PCAP + b"\0" * 4096)})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.dirs(), [])

    def test_oversized_body_rejected_before_reading(self):
        with mock.patch.object(api, "MAX_BODY_BYTES", 100):
            r = self.post({"client_file": ("big.pcap", PCAP + b"\0" * 500)})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.dirs(), [])
        self.db["count_active"].assert_not_called()

    def test_path_traversal_filenames_never_form_paths(self):
        for evil in ("../../../etc/passwd.pcap", "..\\..\\windows\\evil.pcap", "/abs/path/x.pcap", "a\x00b\nc.pcap"):
            with self.subTest(evil=evil):
                r = self.post({"client_file": (evil, PCAP)})
                self.assertEqual(r.status_code, 202, r.text)
                directory = os.path.join(self.root, r.json()["analysis_id"])
                self.assertEqual(os.listdir(directory), ["client.pcap"])
                shown = self.db["insert_analysis"].call_args.args[2]["client"]["display_name"]
                self.assertNotIn("/", shown)
                self.assertNotIn("\\", shown)
                self.assertFalse(shown.startswith("."))
        # Nothing was written outside PCAP_ROOT.
        self.assertTrue(all(len(d) == 36 for d in self.dirs()))

    def test_display_name_sanitizer(self):
        self.assertEqual(api.display_name("../../etc/passwd.pcap"), "passwd.pcap")
        self.assertEqual(api.display_name("C:\\caps\\wan <1>.pcapng"), "wan _1_.pcapng")
        self.assertEqual(api.display_name(""), "capture")

    def test_mode_rules(self):
        cases = [
            ({"client_file": ("c.pcap", PCAP), "server_file": ("s.pcap", PCAP)}, "single", 422),
            ({"client_file": ("c.pcap", PCAP)}, "dual", 422),
            ({"server_file": ("s.pcap", PCAP)}, "dual", 422),
            ({"client_file": ("c.pcap", PCAP)}, "triple", 422),
            ({"client_file": ("c.pcap", PCAP)}, None, 422),
        ]
        for files, mode, status in cases:
            with self.subTest(mode=mode, files=list(files)):
                self.assertEqual(self.post(files, mode=mode).status_code, status)
        self.assertEqual(self.dirs(), [])

    def test_unexpected_or_duplicate_fields(self):
        r = self.post({"client_file": ("c.pcap", PCAP), "other_file": ("x.pcap", PCAP)})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/v1/pcap-analysis", data={"mode": "single"},
                             files=[("client_file", ("a.pcap", PCAP)), ("client_file", ("b.pcap", PCAP))])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.dirs(), [])

    def test_not_multipart(self):
        r = self.client.post("/api/v1/pcap-analysis", content=PCAP, headers={"content-type": "application/octet-stream"})
        self.assertEqual(r.status_code, 415)

    def test_too_many_active(self):
        self.db["count_active"].return_value = api.MAX_ACTIVE
        self.assertEqual(self.post({"client_file": ("c.pcap", PCAP)}).status_code, 429)
        self.assertEqual(self.dirs(), [])

    def test_queue_unavailable(self):
        self.send.side_effect = ConnectionError("redis down")
        r = self.post({"client_file": ("c.pcap", PCAP)})
        self.assertEqual(r.status_code, 503)
        self.db["mark_failed"].assert_called_once()
        self.assertEqual(self.dirs(), [])


class StatusTests(PcapApiTestCase):
    ID = "2b7f5c0e-1d1f-4c3e-9a77-8d1c2a3b4c5d"

    def test_status_queued_completed_failed(self):
        self.db["get_analysis"].return_value = {"id": self.ID, "status": "extracting", "result": None}
        self.assertEqual(self.client.get(f"/api/v1/pcap-analysis/{self.ID}").json()["status"], "extracting")
        self.db["get_analysis"].return_value = {"id": self.ID, "status": "completed", "result": {"findings": [{"id": "F1"}]}}
        self.assertEqual(self.client.get(f"/api/v1/pcap-analysis/{self.ID}").json()["result"]["findings"][0]["id"], "F1")
        self.db["get_analysis"].return_value = {"id": self.ID, "status": "failed", "error": "Not a readable packet capture", "result": None}
        self.assertEqual(self.client.get(f"/api/v1/pcap-analysis/{self.ID}").json()["error"], "Not a readable packet capture")

    def test_not_found_and_invalid_id(self):
        self.db["get_analysis"].return_value = None
        self.assertEqual(self.client.get(f"/api/v1/pcap-analysis/{self.ID}").status_code, 404)
        for bad in ("../../etc", "1", "not-a-uuid"):
            self.assertIn(self.client.get(f"/api/v1/pcap-analysis/{bad}").status_code, (400, 404))

    def test_recent(self):
        self.db["list_recent"].return_value = [{"id": self.ID, "status": "completed"}]
        self.assertEqual(self.client.get("/api/v1/pcap-analysis").json(), [{"id": self.ID, "status": "completed"}])

    def test_delete(self):
        os.makedirs(os.path.join(self.root, self.ID))
        self.db["delete_analysis"].return_value = "deleted"
        r = self.client.delete(f"/api/v1/pcap-analysis/{self.ID}")
        self.assertEqual(r.json(), {"analysis_id": self.ID, "status": "deleted"})
        self.assertFalse(os.path.exists(os.path.join(self.root, self.ID)))
        self.assertEqual(self.audit.call_args.kwargs["event_type"], "pcap_analysis_deleted")
        self.db["delete_analysis"].return_value = "active"
        self.assertEqual(self.client.delete(f"/api/v1/pcap-analysis/{self.ID}").status_code, 409)
        self.db["delete_analysis"].return_value = None
        self.assertEqual(self.client.delete(f"/api/v1/pcap-analysis/{self.ID}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
