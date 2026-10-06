"""
End-to-end Dell OS10 change workflow with NO real switch:

    API precheck route -> worker change_precheck (real Netmiko read of a simulated OS10 CLI)
    -> pre-change backup file + backups row + job -> pending approval (real PostgreSQL)
    -> API approve -> API apply (atomic claim) -> worker apply_approved_change
    -> real ansible-playbook (dellemc.os10 plugins + cli_command) against the simulator
    -> post-check (real Netmiko read) -> applied; Jobs and Audit rows checked.

Also: failed backup, device-rejected write, post-check mismatch, cancellation before apply,
concurrent apply/apply and cancel/apply races, duplicate apply, idempotent precheck, and a
policy rejection; and the safe-L2 flows (access VLAN, trunk conversion, allowed VLANs,
shutdown, uplink / stored-topology / nonexistent-VLAN rejections, post-check mismatch,
cancellation, apply race, state drift before apply) with the topology tables in PostgreSQL. Celery transport is bypassed (queued tasks are executed directly) and
Vault is replaced by test-only credentials for the simulator.

Skipped unless OS10_FLOW_PG_DSN is set and ansible + dellemc.os10 are installed (worker
image + fastapi/httpx/python-multipart). Never point it at a production database.
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
HAVE_ANSIBLE = shutil.which("ansible-playbook") is not None and os.path.isdir(
    "/usr/share/ansible/collections/ansible_collections/dellemc/os10")

SCHEMA = """
DROP TABLE IF EXISTS topology_links, topology_discovery_state, device_lldp_identity CASCADE;
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
INSERT INTO devices VALUES (13, 'Kenda-Core-2', '192.0.2.20', 'dell_os10', true, 'test/simulated');
"""
TOPOLOGY_ROWS = """
INSERT INTO device_lldp_identity (device_id, lldp_system_name, chassis_id) VALUES (13, 'kenda-core-02', 'e8:b5:d0:7a:5c:a3');
"""

CHANGE = {"device_id": 12, "config_parents": ["interface ethernet 1/1/5"], "config_lines": ["description AUTOMATION-TEST"]}


@unittest.skipUnless(DSN and HAVE_ANSIBLE, "needs OS10_FLOW_PG_DSN and ansible + dellemc.os10 (worker image)")
class Os10ChangeFlowTests(unittest.TestCase):
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
                          for name in ("002_approval_cancellation.sql", "003_topology.sql", "004_device_lldp_identity.sql",
                                       "007_jobs_soft_delete.sql")]
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
        import ansible.run_os10 as r10
        import tasks.config_backup as config_backup
        import tasks.dell_os10 as d10
        import yaml
        from fakes.os10_device import TEST_PASSWORD, TEST_USERNAME, FakeOS10

        with self.psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(SCHEMA)
            for migration in self.migrations:
                conn.execute(migration)
            conn.execute(TOPOLOGY_ROWS)

        self.addCleanup(mock.patch.stopall)
        self.device = FakeOS10()
        self.addCleanup(self.device.close)
        inv = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(inv, "hosts.yaml"), "w") as h:
            yaml.safe_dump({"Kenda-Core-1": {"hostname": "127.0.0.1", "port": self.device.port, "platform": "dell_os10",
                                             "groups": ["dell_os10"], "data": {"credential_path": "test/simulated"}}}, h)
        for name, data in (("groups.yaml", {"dell_os10": {}}), ("defaults.yaml", {})):
            with open(os.path.join(inv, name), "w") as h:
                yaml.safe_dump(data, h)
        creds = lambda path: {"username": TEST_USERNAME, "password": TEST_PASSWORD, "secret": ""}
        mock.patch.object(d10, "INVENTORY_DIR", inv).start()
        mock.patch.object(d10, "get_device_credentials", side_effect=creds).start()
        mock.patch.object(r10, "HOSTS_FILE", os.path.join(inv, "hosts.yaml")).start()
        mock.patch.object(r10, "get_device_credentials", side_effect=creds).start()
        self.backup_root = tempfile.mkdtemp(dir=self.tmp)
        mock.patch.object(config_backup, "BACKUP_ROOT", self.backup_root).start()

        # Celery transport bypass: remember what the API queued, run it on demand.
        self.queued = []
        for module in (self.changes, self.approval_actions):
            send = mock.patch.object(module.celery_app, "send_task").start()
            send.side_effect = lambda name, kwargs, **_: self.queued.append((name, kwargs)) or mock.Mock(id="task-id")
        self.trace = []

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

    def step(self, label):
        self.trace.append(f"{label:28} approval={self.approval_status()[1]} device_description="
                          f"{self.device.interfaces['ethernet1/1/5']['description']}")

    def events(self):
        return [r[0] for r in self.sql("SELECT event_type FROM audit_events ORDER BY id")]

    def precheck(self, change=CHANGE):
        r = self.client.post("/api/v1/changes/precheck", json=change)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.queued[-1][0], "network_worker.change_precheck")
        return self.run_queued()

    def precheck_approve(self, change=CHANGE):
        result = self.precheck(change)
        approval_id = result["approval"]["approval_id"]
        r = self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={"approved_by": "reviewer"})
        self.assertEqual(r.status_code, 200, r.text)
        return approval_id

    # ---- tests -------------------------------------------------------------------------
    def test_full_change_lifecycle(self):
        self.step("created")
        result = self.precheck()
        self.assertEqual((result["status"], result["platform"]), ("pending_approval", "dell_os10"))
        self.assertEqual(result["dry_run"]["proposed_changes"], ["description AUTOMATION-TEST"])
        approval_id = result["approval"]["approval_id"]
        self.step("precheck + backup")

        job = self.sql("SELECT job_type, status FROM jobs")
        self.assertEqual(job, [("config_backup", "success")])
        path, checksum = self.sql("SELECT storage_path, checksum FROM backups")[0]
        with open(path) as handle:
            content = handle.read()
        self.assertEqual(hashlib.sha256(content.encode()).hexdigest(), checksum)
        self.assertIn("interface ethernet1/1/5", content)
        self.assertEqual(self.sql("SELECT config_lines, config_parents FROM change_approvals")[0],
                         (["description AUTOMATION-TEST"], ["interface ethernet1/1/5"]))  # canonical form stored

        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={"approved_by": "reviewer"}).status_code, 200)
        self.step("approved")
        r = self.client.post(f"/api/v1/approvals/{approval_id}/apply")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.queued[-1], ("network_worker.apply_approved_change", {"approval_id": approval_id, "claimed_by_api": True}))
        self.step("apply claimed")
        applied = self.run_queued()
        self.step("apply + post-check")

        self.assertEqual((applied["status"], applied["write_skipped"]), ("applied", False))
        self.assertEqual(self.approval_status()[1], "applied")
        self.assertEqual(self.device.interfaces["ethernet1/1/5"]["description"], "AUTOMATION-TEST")
        writes = self.device.log[self.device.log.index("configure terminal"):]
        self.assertEqual(writes[:4], ["configure terminal", "interface ethernet1/1/5", "description AUTOMATION-TEST", "end"])
        self.assertEqual(self.events(), ["precheck_requested", "backup_started", "backup_completed", "precheck_completed",
                                         "approval_approved", "apply_requested", "apply_started", "apply_completed"])
        jobs = self.client.get("/api/v1/jobs").json()
        self.assertEqual([(j["job_type"], j["status"]) for j in jobs], [("config_backup", "success")])
        print("\n    OS10 change state trace:\n      " + "\n      ".join(self.trace))
        print("    audit:", " -> ".join(self.events()))

    def test_backup_failure_creates_no_approval(self):
        mock.patch("tasks.config_backup.BACKUP_ROOT", os.path.join(self.backup_root, "not-a-dir.txt")).start()
        open(os.path.join(self.backup_root, "not-a-dir.txt"), "w").close()
        with self.assertRaises(Exception):
            self.precheck()
        self.assertEqual(self.sql("SELECT count(*) FROM change_approvals")[0][0], 0)
        self.assertEqual(self.sql("SELECT status FROM jobs"), [("failed",)])
        self.assertIn("backup_failed", self.events())
        self.assertIn("precheck_failed", self.events())
        self.assertNotIn("configure terminal", self.device.log)

    def test_device_rejected_write_fails_and_keeps_backup(self):
        approval_id = self.precheck_approve()
        self.device.reject = {"description AUTOMATION-TEST"}
        self.client.post(f"/api/v1/approvals/{approval_id}/apply")
        with self.assertRaisesRegex(Exception, "Ansible apply failed .*% Error: Command rejected"):
            self.run_queued()
        self.assertEqual(self.approval_status()[1], "failed")
        self.assertEqual(self.events()[-1], "apply_failed")
        self.assertEqual(self.sql("SELECT count(*) FROM backups")[0][0], 1)
        self.assertIsNone(self.device.interfaces["ethernet1/1/5"]["description"])

    def test_postcheck_mismatch_fails(self):
        approval_id = self.precheck_approve()
        self.device.ignore_writes = True
        self.client.post(f"/api/v1/approvals/{approval_id}/apply")
        with self.assertRaisesRegex(Exception, "Post-check failed"):
            self.run_queued()
        self.assertEqual(self.approval_status()[1], "failed")
        message = self.sql("SELECT message FROM audit_events WHERE event_type = 'apply_failed'")[0][0]
        self.assertIn("Post-check failed", message)

    def test_cancelled_change_cannot_apply(self):
        approval_id = self.precheck_approve()
        r = self.client.post(f"/api/v1/approvals/{approval_id}/cancel", json={"cancelled_by": "operator"})
        self.assertEqual(r.json()["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code, 409)
        with self.assertRaisesRegex(ValueError, "is cancelled"):  # a stray message never reaches the device
            self.worker.app.tasks["network_worker.apply_approved_change"](approval_id=approval_id, claimed_by_api=True)
        self.assertNotIn("configure terminal", self.device.log)

    def test_concurrent_apply_requests_claim_once(self):
        approval_id = self.precheck_approve()
        codes = []
        threads = [threading.Thread(target=lambda: codes.append(self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code))
                   for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(codes), [202, 409, 409, 409])
        self.assertEqual(len([q for q in self.queued if q[0] == "network_worker.apply_approved_change"]), 1)

    def test_cancel_versus_apply_race_has_one_winner(self):
        approval_id = self.precheck_approve()
        out = {}
        a = threading.Thread(target=lambda: out.update(apply=self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code))
        c = threading.Thread(target=lambda: out.update(cancel=self.client.post(f"/api/v1/approvals/{approval_id}/cancel",
                                                                               json={"cancelled_by": "operator"}).status_code))
        a.start(); c.start(); a.join(); c.join()
        self.assertEqual(sorted(out.values()), [200, 409] if out["cancel"] == 200 else [202, 409])
        final = self.approval_status()[1]
        self.assertEqual(final, "cancelled" if out["cancel"] == 200 else "applying")
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/cancel", json={"cancelled_by": "x2"}).status_code, 409)

    def test_applied_change_cannot_be_reapplied(self):
        approval_id = self.precheck_approve()
        self.client.post(f"/api/v1/approvals/{approval_id}/apply")
        self.run_queued()
        writes_before = self.device.log.count("configure terminal")
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code, 409)
        with self.assertRaisesRegex(ValueError, "is applied"):  # broker redelivery
            self.worker.app.tasks["network_worker.apply_approved_change"](approval_id=approval_id, claimed_by_api=True)
        self.assertEqual(self.device.log.count("configure terminal"), writes_before)
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/cancel", json={"cancelled_by": "operator"}).status_code, 409)

    def test_idempotent_precheck_when_already_present(self):
        self.device.interfaces["ethernet1/1/5"]["description"] = "AUTOMATION-TEST"
        result = self.precheck()
        self.assertEqual(result["status"], "no_change_required")
        self.assertEqual(self.sql("SELECT count(*) FROM change_approvals")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM backups")[0][0], 0)

    def test_policy_rejection(self):
        with self.assertRaisesRegex(ValueError, "Rejected by OS10 change policy"):
            self.precheck({**CHANGE, "config_lines": ["reload"]})
        self.assertEqual(self.sql("SELECT count(*) FROM change_approvals")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM jobs")[0][0], 0)
        self.assertEqual(self.events(), ["precheck_requested", "precheck_failed"])
        self.assertNotIn("show running-configuration", self.device.log)  # rejected before any device access


    # ---- safe-L2 (V1.1) ------------------------------------------------------------------
    def l2(self, interface, *lines):
        return {"device_id": 12, "config_parents": [f"interface ethernet {interface}"], "config_lines": list(lines)}

    def apply(self, approval_id):
        r = self.client.post(f"/api/v1/approvals/{approval_id}/apply")
        self.assertEqual(r.status_code, 202, r.text)
        return self.run_queued()

    def port(self, name="ethernet1/1/18"):
        return self.device.interfaces[name]

    def test_l2_a_access_vlan_lifecycle(self):
        result = self.precheck(self.l2("1/1/18", "switchport access vlan 30"))
        self.assertEqual(result["status"], "pending_approval")
        self.assertEqual(result["dry_run"]["change_plan"], ["access VLAN: 20 → 30"])
        self.assertEqual(self.sql("SELECT config_lines, config_parents FROM change_approvals")[0],
                         (["switchport access vlan 30"], ["interface ethernet1/1/18"]))
        approval_id = result["approval"]["approval_id"]
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/approve", json={"approved_by": "reviewer"}).status_code, 200)
        applied = self.apply(approval_id)
        self.assertEqual((applied["status"], applied["sent_lines"]), ("applied", ["switchport access vlan 30"]))
        self.assertEqual((self.approval_status()[1], self.port()["access_vlan"]), ("applied", 30))
        writes = self.device.log[self.device.log.index("configure terminal"):]
        self.assertEqual(writes[:4], ["configure terminal", "interface ethernet1/1/18", "switchport access vlan 30", "end"])
        self.assertEqual(self.events(), ["precheck_requested", "backup_started", "backup_completed", "precheck_completed",
                                         "approval_approved", "apply_requested", "apply_started", "apply_completed"])
        self.assertEqual([(j["job_type"], j["status"]) for j in self.client.get("/api/v1/jobs").json()],
                         [("config_backup", "success")])

    def test_l2_b_trunk_conversion(self):
        approval_id = self.precheck_approve(self.l2("1/1/18", "switchport mode trunk", "switchport trunk allowed vlan 30,40"))
        self.apply(approval_id)
        self.assertEqual((self.port()["mode"], self.port()["allowed"], self.port()["access_vlan"]), ("trunk", {30, 40}, 20))

    def test_l2_c_allowed_vlan_update(self):
        approval_id = self.precheck_approve(self.l2("1/1/19", "switchport trunk allowed vlan remove 10"))
        self.assertEqual(self.sql("SELECT config_lines FROM change_approvals")[0][0], ["no switchport trunk allowed vlan 10"])
        self.apply(approval_id)
        self.assertEqual(self.port("ethernet1/1/19")["allowed"], {20})

    def test_l2_e_shutdown_safe_port(self):
        approval_id = self.precheck_approve(self.l2("1/1/18", "shutdown"))
        self.apply(approval_id)
        self.assertEqual(self.port()["admin"], "down")

    def test_l2_f_shutdown_uplink_blocked(self):
        result = self.precheck(self.l2("1/1/25", "shutdown"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rejection_reasons"], [
            "shutdown rejected: ethernet1/1/25 is classified as an uplink (live LLDP: kenda-core-02 ethernet1/1/25 = "
            "inventory device Kenda-Core-2)."])
        self.assertEqual(self.sql("SELECT count(*) FROM change_approvals")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM jobs")[0][0], 0)
        self.assertEqual(self.events(), ["precheck_requested", "precheck_failed"])
        self.assertNotIn("configure terminal", self.device.log)

    def test_l2_stored_topology_blocks_shutdown(self):
        with self.psycopg.connect(DSN, autocommit=True) as conn:
            # Core-2 reported a link TO Core-1 ethernet1/1/18 (Core-1's own LLDP shows nothing there).
            conn.execute("INSERT INTO topology_links (local_device_id, local_interface, remote_device_id, remote_system_name, "
                         "remote_interface, first_seen_at, last_seen_at, active) VALUES (13, 'ethernet1/1/18', 12, "
                         "'kenda-core-01', 'ethernet1/1/18', now(), now(), true)")
        result = self.precheck(self.l2("1/1/18", "shutdown"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rejection_reasons"], [
            "shutdown rejected: ethernet1/1/18 is classified as an uplink (stored topology: Kenda-Core-2 ethernet1/1/18 = "
            "inventory device Kenda-Core-2)."])

    def test_l2_stale_stored_link_is_ignored(self):
        with self.psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("INSERT INTO topology_links (local_device_id, local_interface, remote_system_name, first_seen_at, "
                         "last_seen_at, active) VALUES (12, 'ethernet1/1/18', 'old-server', now() - interval '9 days', "
                         "now() - interval '3 days', false)")
        self.assertEqual(self.precheck(self.l2("1/1/18", "shutdown"))["status"], "pending_approval")

    def test_l2_g_nonexistent_vlan(self):
        result = self.precheck(self.l2("1/1/18", "switchport access vlan 200"))
        self.assertEqual((result["status"], result["rejection_reasons"]), ("rejected", ["VLAN 200 does not exist on device."]))
        self.assertEqual(self.sql("SELECT count(*) FROM backups")[0][0], 0)

    def test_l2_h_postcheck_mismatch(self):
        approval_id = self.precheck_approve(self.l2("1/1/18", "switchport access vlan 30"))
        self.device.ignore_writes = True
        with self.assertRaisesRegex(Exception, "Post-check failed"):
            self.apply(approval_id)
        self.assertEqual(self.approval_status()[1], "failed")
        self.assertEqual(self.events()[-1], "apply_failed")

    def test_l2_i_cancellation_before_apply(self):
        approval_id = self.precheck_approve(self.l2("1/1/18", "shutdown"))
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/cancel", json={"cancelled_by": "operator"}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code, 409)
        self.assertNotIn("configure terminal", self.device.log)
        self.assertEqual(self.port()["admin"], "up")

    def test_l2_j_duplicate_apply_race(self):
        approval_id = self.precheck_approve(self.l2("1/1/18", "shutdown"))
        codes = []
        threads = [threading.Thread(target=lambda: codes.append(self.client.post(f"/api/v1/approvals/{approval_id}/apply").status_code))
                   for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(codes), [202, 409, 409, 409])
        self.run_queued()
        self.assertEqual(self.device.log.count("configure terminal"), 1)
        with self.assertRaisesRegex(ValueError, "is applied"):
            self.worker.app.tasks["network_worker.apply_approved_change"](approval_id=approval_id, claimed_by_api=True)
        self.assertEqual(self.device.log.count("configure terminal"), 1)

    def test_l2_state_drift_blocks_apply(self):
        approval_id = self.precheck_approve(self.l2("1/1/18", "shutdown"))
        self.device.lldp["ethernet1/1/18"] = ("kenda-core-02", "ethernet1/1/18", "e8:b5:d0:7a:5c:a3")
        with self.assertRaisesRegex(Exception, "Pre-apply safety check failed; nothing was sent: shutdown rejected: "
                                               "ethernet1/1/18 is classified as an uplink"):
            self.apply(approval_id)
        self.assertNotIn("configure terminal", self.device.log)
        self.assertEqual(self.approval_status()[1], "failed")
        message = self.sql("SELECT message FROM audit_events WHERE event_type = 'apply_failed'")[0][0]
        self.assertIn("Pre-apply safety check failed", message)


if __name__ == "__main__":
    unittest.main()
