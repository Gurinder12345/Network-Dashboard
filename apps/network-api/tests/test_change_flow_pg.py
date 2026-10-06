"""
End-to-end multi-block change workflow for Dell OS10 AND Dell OS6 with NO real switch:

    API precheck route (blocks) -> worker change_precheck (real Netmiko read of a simulated
    CLI) -> pre-change backup file + backups row + job -> one pending approval with
    config_blocks (real PostgreSQL, migrations 002/007/008) -> API approve -> API apply
    (atomic claim) -> worker apply_approved_change -> real ansible-playbook
    (config_blocks_apply.yml, dellemc.os6/os10 plugins) -> per-block execution result ->
    post-check (real Netmiko read) + verification commands -> applied / failed;
    config_apply job, approval view (blocks, CLI preview, execution_result), audit trail.

Also: precheck failure (all-or-nothing), failure in the middle block (partial apply,
later blocks never sent), post-check mismatch, backup failure, cancellation before apply,
concurrent apply/apply and cancel/apply races, duplicate apply, and historical
single-parent rows. Celery transport is bypassed (queued tasks are executed directly) and
Vault is replaced by test-only credentials for the simulators.

Skipped unless OS10_FLOW_PG_DSN is set and ansible + dellemc collections are installed
(worker image + fastapi/httpx/python-multipart). Never point it at a production database.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlparse

DSN = os.getenv("OS10_FLOW_PG_DSN")
HERE = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.dirname(HERE)
WORKER_DIR = os.path.join(os.path.dirname(API_DIR), "network-worker")
REPO = os.path.dirname(os.path.dirname(API_DIR))
HAVE_ANSIBLE = shutil.which("ansible-playbook") is not None and all(
    os.path.isdir(f"/usr/share/ansible/collections/ansible_collections/dellemc/{c}") for c in ("os6", "os10"))

SCHEMA = """
DROP TABLE IF EXISTS audit_events, change_approvals, backups, jobs, devices CASCADE;
CREATE TABLE devices (id bigserial PRIMARY KEY, hostname varchar NOT NULL, management_ip inet NOT NULL, platform varchar NOT NULL,
    enabled boolean NOT NULL DEFAULT true, credential_path varchar);
CREATE TABLE jobs (id uuid PRIMARY KEY, device_id bigint REFERENCES devices(id), job_type varchar, status varchar, requested_by varchar,
    started_at timestamptz, finished_at timestamptz, error_message text);
CREATE TABLE backups (id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id), device_id bigint REFERENCES devices(id), backup_type varchar,
    storage_path text, checksum varchar, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE change_approvals (id uuid PRIMARY KEY, device_id bigint NOT NULL REFERENCES devices(id), backup_job_id uuid NOT NULL REFERENCES jobs(id),
    requested_by varchar NOT NULL, approved_by varchar, status varchar NOT NULL DEFAULT 'pending', config_lines jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), approved_at timestamptz, config_parents jsonb);
CREATE TABLE audit_events (id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id), device_id bigint REFERENCES devices(id),
    event_type varchar NOT NULL, message text, created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO devices VALUES (12, 'Kenda-Core-1', '192.0.2.10', 'dell_os10', true, 'test/simulated');
INSERT INTO devices VALUES (1, 'Kenda-HARO-IDF-A', '192.0.2.31', 'dell_os6', true, 'test/simulated');
"""

OS10_BLOCKS = [
    {"parent": "interface ethernet 1/1/18", "commands": ["description APP-SERVER", "switchport mode trunk",
                                                          "switchport trunk allowed vlan 10,20,30"]},
    {"parent": "interface ethernet1/1/19", "commands": ["description USER-PC", "switchport mode access", "switchport access vlan 40"]},
    {"parent": "interface vlan50", "commands": ["description USERS"]},
    {"parent": "router ospf 1", "commands": ["router-id 192.0.2.1"]},
    {"parent": None, "commands": ["ip domain-name lab.example"]},
]
OS6_BLOCKS = [
    {"parent": "interface Tw1/0/3", "commands": ["description APP-SERVER", "switchport mode trunk"]},
    {"parent": "interface Tw1/0/4", "commands": ["description USER-PC", "switchport access vlan 20"]},
    {"parent": "vlan 50", "commands": ["name USERS"]},
    {"parent": None, "commands": ["ip routing"]},
]


@unittest.skipUnless(DSN and HAVE_ANSIBLE, "needs OS10_FLOW_PG_DSN and ansible + dellemc collections (worker image)")
class ChangeFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path[:0] = [API_DIR, WORKER_DIR, os.path.join(WORKER_DIR, "tests")]
        import psycopg
        from fastapi.testclient import TestClient

        from app import approval_actions, changes
        from app.db import client as api_db
        from app.main import app
        from db import client as worker_db
        import worker

        url = urlparse(DSN)
        for module in (api_db, worker_db):
            module.DB_HOST, module.DB_PORT, module.DB_NAME = url.hostname, str(url.port or 5432), url.path.lstrip("/")
            module.DB_USER, module.DB_PASSWORD = url.username, url.password
        cls.migrations = [open(os.path.join(REPO, "db", "migrations", name)).read()
                          for name in ("002_approval_cancellation.sql", "007_jobs_soft_delete.sql", "008_change_config_blocks.sql")]
        cls.psycopg, cls.worker, cls.client = psycopg, worker, TestClient(app)
        cls.changes, cls.approval_actions = changes, approval_actions
        cls.tmp = tempfile.mkdtemp()
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)  # nornir.log

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import ansible.run_os6 as r6
        import ansible.run_os10 as r10
        import tasks.config_backup as config_backup
        import tasks.dell_os6 as d6
        import tasks.dell_os10 as d10
        import yaml
        from fakes.os6_device import FakeOS6
        from fakes.os10_device import FakeOS10
        from fakes.switch import TEST_PASSWORD, TEST_USERNAME

        with self.psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(SCHEMA)
            for migration in self.migrations:
                conn.execute(migration)

        self.addCleanup(mock.patch.stopall)
        self.os10, self.os6 = FakeOS10(), FakeOS6()
        self.addCleanup(self.os10.close)
        self.addCleanup(self.os6.close)
        inv = tempfile.mkdtemp(dir=self.tmp)
        hosts = {"Kenda-Core-1": {"hostname": "127.0.0.1", "port": self.os10.port, "platform": "dell_os10", "groups": ["dell_os10"],
                                  "data": {"credential_path": "test/simulated"}},
                 "Kenda-HARO-IDF-A": {"hostname": "127.0.0.1", "port": self.os6.port, "platform": "dell_os6", "groups": ["dell_os6"],
                                      "data": {"credential_path": "test/simulated"}}}
        with open(os.path.join(inv, "hosts.yaml"), "w") as h:
            yaml.safe_dump(hosts, h)
        for name, data in (("groups.yaml", {"dell_os10": {}, "dell_os6": {}}), ("defaults.yaml", {})):
            with open(os.path.join(inv, name), "w") as h:
                yaml.safe_dump(data, h)
        creds = lambda path: {"username": TEST_USERNAME, "password": TEST_PASSWORD, "secret": ""}
        self.password = TEST_PASSWORD
        for module in (d6, d10):
            mock.patch.object(module, "INVENTORY_DIR", inv).start()
            mock.patch.object(module, "get_device_credentials", side_effect=creds).start()
        for module in (r6, r10):
            mock.patch.object(module, "HOSTS_FILE", os.path.join(inv, "hosts.yaml")).start()
            mock.patch.object(module, "get_device_credentials", side_effect=creds).start()
        self.backup_root = tempfile.mkdtemp(dir=self.tmp)
        mock.patch.object(config_backup, "BACKUP_ROOT", self.backup_root).start()

        # Celery transport bypass: remember what the API queued, run it on demand.
        self.queued = []
        for module in (self.changes, self.approval_actions):
            send = mock.patch.object(module.celery_app, "send_task").start()
            send.side_effect = lambda name, kwargs, **_: self.queued.append((name, kwargs)) or mock.Mock(id="task-id")

    # ---- helpers -----------------------------------------------------------------------
    def sql(self, statement, *args):
        with self.psycopg.connect(DSN) as conn:
            return conn.execute(statement, args).fetchall()

    def run_queued(self):
        name, kwargs = self.queued.pop(0)
        return self.worker.app.tasks[name](**kwargs)

    def approval_status(self):
        rows = self.sql("SELECT id, status FROM change_approvals")
        return (str(rows[0][0]), rows[0][1]) if rows else (None, None)

    def events(self):
        return [r[0] for r in self.sql("SELECT event_type FROM audit_events ORDER BY id")]

    def precheck(self, body):
        r = self.client.post("/api/v1/changes/precheck", json=body)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.queued[-1][0], "network_worker.change_precheck")
        return self.run_queued()

    def precheck_approve(self, body):
        result = self.precheck(body)
        self.assertEqual(result["status"], "pending_approval", result.get("rejection_reasons"))
        approval_id = result["approval"]["approval_id"]
        r = self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={"approved_by": "reviewer"})
        self.assertEqual(r.status_code, 200, r.text)
        return approval_id

    def apply(self, approval_id):
        r = self.client.post(f"/api/v1/approvals/{approval_id}/apply")
        self.assertEqual(r.status_code, 202, r.text)
        return self.run_queued()

    def approval_view(self, approval_id):
        return next(a for a in self.client.get("/api/v1/approvals").json() if a["id"] == approval_id)

    # ---- full lifecycle, both platforms --------------------------------------------------
    def lifecycle(self, device_id, blocks, device, configure):
        approval_id = self.precheck_approve({"device_id": device_id, "blocks": blocks, "verification_commands": ["show vlan"]})
        path, checksum = self.sql("SELECT storage_path, checksum FROM backups")[0]
        with open(path) as handle:
            self.assertEqual(hashlib.sha256(handle.read().encode()).hexdigest(), checksum)

        view = self.approval_view(approval_id)
        self.assertEqual((view["status"], view["block_count"], view["legacy_single_block"]), ("approved", len(blocks), False))
        self.assertEqual([b["parent"] for b in view["config_blocks"]],
                         [" ".join(b["parent"].split()) if b["parent"] else None for b in blocks])
        self.assertTrue(view["cli_preview"].startswith("! Block 1\n"))
        self.assertEqual(view["precheck_summary"]["overall"], "PASS")
        self.assertEqual(view["verification_commands"], ["show vlan"])

        result = self.apply(approval_id)
        self.assertEqual(result["status"], "applied")
        expected = []
        for block in view["config_blocks"]:
            expected += [configure] + ([block["parent"]] if block["parent"] else []) + block["commands"] + ["end"]
        self.assertEqual(device.config_writes()[:len(expected)], expected)  # exact block + command order

        view = self.approval_view(approval_id)
        execution = view["execution_result"]
        self.assertEqual((view["status"], execution["outcome"], execution["execution"]), ("applied", "applied", "success"))
        self.assertEqual([b["status"] for b in execution["blocks"]], ["applied"] * len(blocks))
        self.assertEqual(execution["verification"][0]["command"], "show vlan")
        jobs = {(j["job_type"], j["status"]) for j in self.client.get("/api/v1/jobs").json()}
        self.assertEqual(jobs, {("config_backup", "success"), ("config_apply", "success")})
        events = self.events()
        self.assertEqual(events[:6], ["precheck_requested", "backup_started", "backup_completed", "precheck_completed",
                                      "change_created", "approval_approved"])
        self.assertEqual(events[6:], ["apply_requested", "apply_started"] + ["apply_block_completed"] * len(blocks)
                         + ["postcheck_completed", "apply_completed"])
        self.assertNotIn(self.password, " ".join(r[0] or "" for r in self.sql("SELECT message FROM audit_events")))
        return view

    def test_os10_multi_block_lifecycle(self):
        self.lifecycle(12, OS10_BLOCKS, self.os10, "configure terminal")
        self.assertEqual(self.os10.interfaces["ethernet1/1/18"]["allowed"], {10, 20, 30})
        self.assertEqual(self.os10.contexts["router ospf 1"], ["router-id 192.0.2.1"])

    def test_os6_multi_block_lifecycle(self):
        self.lifecycle(1, OS6_BLOCKS, self.os6, "configure")
        self.assertEqual(self.os6.interfaces["Tw1/0/4"]["description"], "USER-PC")
        self.assertEqual(self.os6.contexts["vlan 50"], ["name USERS"])
        self.assertEqual(self.os6.global_lines, ["ip routing"])

    # ---- failures ---------------------------------------------------------------------------
    def test_partial_apply_os10(self):
        self.partial_apply(12, OS10_BLOCKS, self.os10)

    def test_partial_apply_os6(self):
        self.partial_apply(1, OS6_BLOCKS, self.os6)

    def partial_apply(self, device_id, blocks, device):
        approval_id = self.precheck_approve({"device_id": device_id, "blocks": blocks})
        bad = blocks[1]["commands"][1]
        device.reject = {bad}
        with self.assertRaisesRegex(Exception, "PARTIAL APPLY"):
            self.apply(approval_id)
        view = self.approval_view(approval_id)
        execution = view["execution_result"]
        self.assertEqual((view["status"], execution["outcome"], execution["execution"]), ("failed", "partial_apply", "partial"))
        self.assertEqual([b["status"] for b in execution["blocks"]], ["applied", "failed"] + ["not_attempted"] * (len(blocks) - 2))
        self.assertEqual(execution["blocks"][1]["failed_command"], 2)
        after = device.log[device.log.index(bad) + 1:]
        self.assertFalse(any(c in after for b in blocks[2:] for c in b["commands"]))  # later blocks never sent
        job = [j for j in self.client.get("/api/v1/jobs").json() if j["job_type"] == "config_apply"][0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("PARTIAL APPLY", job["error_message"])
        events = self.events()
        self.assertIn("apply_block_failed", events)
        self.assertEqual(events.count("apply_block_not_attempted"), len(blocks) - 2)
        self.assertEqual(events[-1], "apply_failed")

    def test_precheck_failure_is_all_or_nothing(self):
        result = self.precheck({"device_id": 12, "blocks": OS10_BLOCKS + [
            {"parent": "interface ethernet1/1/99", "commands": ["description NOPE"]}]})
        self.assertEqual(result["status"], "precheck_failed")
        self.assertEqual([b["status"] for b in result["dry_run"]["blocks"]], ["PASS"] * 5 + ["FAILED"])
        self.assertEqual(self.sql("SELECT count(*) FROM change_approvals")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM jobs")[0][0], 0)
        self.assertEqual(self.events(), ["precheck_requested", "precheck_failed"])

    def test_postcheck_mismatch_fails(self):
        approval_id = self.precheck_approve({"device_id": 12, "blocks": [
            {"parent": "interface ethernet1/1/18", "commands": ["description POSTCHECK"]}]})
        self.os10.ignore_writes = True  # the device accepts the command but keeps the old state
        with self.assertRaisesRegex(Exception, "Post-check failed"):
            self.apply(approval_id)
        execution = self.approval_view(approval_id)["execution_result"]
        self.assertEqual((execution["execution"], execution["semantic"]), ("success", "failed"))
        self.assertIn("postcheck_failed", self.events())

    def test_backup_failure_creates_no_approval(self):
        mock.patch("tasks.config_backup.BACKUP_ROOT", os.path.join(self.backup_root, "not-a-dir.txt")).start()
        open(os.path.join(self.backup_root, "not-a-dir.txt"), "w").close()
        with self.assertRaises(Exception):
            self.precheck({"device_id": 1, "blocks": OS6_BLOCKS})
        self.assertEqual(self.sql("SELECT count(*) FROM change_approvals")[0][0], 0)
        self.assertEqual(self.sql("SELECT status FROM jobs"), [("failed",)])
        self.assertNotIn("configure", self.os6.log)

    # ---- approval / claim protections -------------------------------------------------------
    def test_cancelled_change_cannot_apply(self):
        approval_id = self.precheck_approve({"device_id": 1, "blocks": OS6_BLOCKS})
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/cancel", json={"cancelled_by": "operator"}).json()["status"],
                         "cancelled")
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code, 409)
        with self.assertRaisesRegex(ValueError, "is cancelled"):  # a stray message never reaches the device
            self.worker.app.tasks["network_worker.apply_approved_change"](approval_id=approval_id, claimed_by_api=True)
        self.assertNotIn("configure", self.os6.log)

    def test_concurrent_apply_requests_claim_once(self):
        approval_id = self.precheck_approve({"device_id": 12, "blocks": OS10_BLOCKS})
        codes = []
        threads = [threading.Thread(target=lambda: codes.append(self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code))
                   for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(codes), [202, 409, 409, 409])
        self.assertEqual(len([q for q in self.queued if q[0] == "network_worker.apply_approved_change"]), 1)

    def test_cancel_versus_apply_race_has_one_winner(self):
        approval_id = self.precheck_approve({"device_id": 12, "blocks": OS10_BLOCKS})
        out = {}
        a = threading.Thread(target=lambda: out.update(apply=self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code))
        c = threading.Thread(target=lambda: out.update(cancel=self.client.post(f"/api/v1/approvals/{approval_id}/cancel",
                                                                               json={"cancelled_by": "operator"}).status_code))
        a.start(); c.start(); a.join(); c.join()
        self.assertEqual(sorted(out.values()), [200, 409] if out["cancel"] == 200 else [202, 409])
        self.assertEqual(self.approval_status()[1], "cancelled" if out["cancel"] == 200 else "applying")

    def test_duplicate_apply_and_redelivery_execute_once(self):
        approval_id = self.precheck_approve({"device_id": 12, "blocks": OS10_BLOCKS})
        r = self.client.post(f"/api/v1/approvals/{approval_id}/apply")
        self.assertEqual(r.status_code, 202)
        task = self.queued.pop(0)
        # A redelivered copy of the same message while the first is still "applying":
        with self.psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("UPDATE change_approvals SET execution_result = '{\"outcome\": \"applying\"}' WHERE id = %s", (approval_id,))
        with self.assertRaisesRegex(ValueError, "already has an apply attempt"):
            self.worker.app.tasks[task[0]](**task[1])
        self.assertNotIn("configure terminal", self.os10.log)
        with self.psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("UPDATE change_approvals SET execution_result = NULL WHERE id = %s", (approval_id,))
        self.worker.app.tasks[task[0]](**task[1])
        writes = self.os10.log.count("configure terminal")
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code, 409)
        with self.assertRaisesRegex(ValueError, "is applied"):
            self.worker.app.tasks[task[0]](**task[1])
        self.assertEqual(self.os10.log.count("configure terminal"), writes)

    # ---- compatibility -----------------------------------------------------------------------
    def test_historical_single_parent_row_still_displays(self):
        with self.psycopg.connect(DSN, autocommit=True) as conn:
            job = conn.execute("INSERT INTO jobs (id, device_id, job_type, status) VALUES (gen_random_uuid(), 1, 'config_backup', "
                               "'success') RETURNING id").fetchone()[0]
            conn.execute("INSERT INTO change_approvals (id, device_id, backup_job_id, requested_by, status, config_lines, config_parents) "
                         "VALUES (gen_random_uuid(), 1, %s, 'system', 'applied', '[\"description OLD\"]', '[\"interface Tw1/0/3\"]')",
                         (job,))
        view = self.client.get("/api/v1/approvals").json()[0]
        self.assertEqual((view["legacy_single_block"], view["block_count"], view["command_count"]), (True, 1, 1))
        self.assertEqual(view["config_blocks"], [{"order": 0, "parent": "interface Tw1/0/3", "commands": ["description OLD"]}])
        self.assertEqual(view["cli_preview"], "! Block 1\ninterface Tw1/0/3\n description OLD")
        self.assertIsNone(view["execution_result"])

    def test_legacy_request_form_still_works(self):
        approval_id = self.precheck_approve({"device_id": 1, "config_parents": ["interface Tw1/0/4"], "config_lines": ["description LEGACY"]})
        self.apply(approval_id)
        self.assertEqual(self.os6.interfaces["Tw1/0/4"]["description"], "LEGACY")


if __name__ == "__main__":
    unittest.main()
