"""
Multi-block change workflow with the REAL Netmiko readers and the REAL Ansible playbook
(ansible/playbooks/config_blocks_apply.yml; dellemc.os6 / dellemc.os10 connection plugins +
ansible.netcommon.cli_command) against simulated OS6 and OS10 CLIs on 127.0.0.1
(tests/fakes). No switch is contacted. Needs the worker image (ansible-playbook +
collections). DB/audit/job calls are mocked; the real-PostgreSQL flow is in the API's
test_os10_change_flow_pg.py.
    python -m unittest tests.test_change_simulated -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from fakes.os6_device import FakeOS6  # noqa: E402
from fakes.os10_device import FakeOS10  # noqa: E402
from fakes.switch import TEST_PASSWORD, TEST_USERNAME  # noqa: E402

HAVE_ANSIBLE = shutil.which("ansible-playbook") is not None and all(
    os.path.isdir(f"/usr/share/ansible/collections/ansible_collections/dellemc/{c}") for c in ("os6", "os10"))


class SimulatedFlow:
    """Shared tests; subclasses set FAKE, PLATFORM, HOST and the platform's CLI examples."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.cwd = os.getcwd()
        os.chdir(cls.tmp)  # nornir writes nornir.log to the working directory

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import ansible.run_os6 as r6
        import ansible.run_os10 as r10
        import tasks.dell_os6 as d6
        import tasks.dell_os10 as d10
        from changes import workflow

        self.addCleanup(mock.patch.stopall)
        self.workflow = workflow
        self.device = self.FAKE()
        self.addCleanup(self.device.close)
        self.inv = tempfile.mkdtemp(dir=self.tmp)
        host = {self.HOST: {"hostname": "127.0.0.1", "port": self.device.port, "platform": self.PLATFORM,
                            "groups": [self.PLATFORM], "data": {"credential_path": "test/simulated"}}}
        with open(os.path.join(self.inv, "hosts.yaml"), "w") as h:
            yaml.safe_dump(host, h)
        for name, data in (("groups.yaml", {self.PLATFORM: {}}), ("defaults.yaml", {})):
            with open(os.path.join(self.inv, name), "w") as h:
                yaml.safe_dump(data, h)
        creds = lambda path: {"username": TEST_USERNAME, "password": TEST_PASSWORD, "secret": ""}
        for module in (d6, d10):
            mock.patch.object(module, "INVENTORY_DIR", self.inv).start()
            mock.patch.object(module, "get_device_credentials", side_effect=creds).start()
        for module in (r6, r10):
            mock.patch.object(module, "HOSTS_FILE", os.path.join(self.inv, "hosts.yaml")).start()
            mock.patch.object(module, "get_device_credentials", side_effect=creds).start()

        p = lambda name, **kw: mock.patch.object(workflow, name, **kw).start()
        from fakes import fake_redis

        fake_redis.install(self)  # in-memory device coordination
        # Pre-apply backup and post-change health write to PostgreSQL / probe the real
        # management IP; both are covered by test_coordination.py and the API PG flow.
        p("_pre_apply_backup", return_value={"status": "success", "job_id": "pre-1"})
        p("_post_change_health", return_value={"ok": True, "status": "healthy"})
        p("release_apply_claim", return_value=True)
        self.dev = {"id": 12, "hostname": self.HOST, "platform": self.PLATFORM, "management_ip": "192.0.2.10"}
        p("get_device_by_hostname", return_value=self.dev)
        p("get_device_by_id", return_value=self.dev)
        self.audit = p("create_audit_event")
        p("verify_backup_for_device", return_value={"valid": True})
        self.create_approval = p("create_change_approval", return_value={"approval_id": "a1", "status": "pending"})
        self.applied, self.failed = p("mark_approval_applied"), p("mark_approval_failed")
        p("begin_execution", return_value=True)
        self.results = []
        p("set_execution_result", side_effect=lambda aid, r: self.results.append(dict(r)))
        p("create_job", return_value="job-1")
        self.job_ok, self.job_failed = p("mark_job_success"), p("mark_job_failed")
        self.backup = mock.Mock(return_value={"status": "success", "job_id": "b1"})

    def precheck(self, blocks, verification=None):
        return self.workflow.run_precheck(self.HOST, config_blocks=blocks, verification_commands=verification,
                                          backup=self.backup)

    def apply(self):
        stored = self.create_approval.call_args.kwargs
        mock.patch.object(self.workflow, "get_change_approval", return_value={
            "approval_id": "a1", "device_id": 12, "backup_job_id": "b1", "status": "applying", "approved_by": "reviewer",
            "config_lines": stored["config_lines"], "config_parents": None, "config_blocks": stored["config_blocks"],
            "verification_commands": stored["verification_commands"], "execution_result": None}).start()
        return self.workflow.run_apply("a1", claimed_by_api=True)

    def events(self):
        return [c.kwargs["event_type"] for c in self.audit.call_args_list]

    def expected_writes(self, blocks):
        lines = []
        for block in blocks:
            lines.append(self.FAKE.configure_commands[0])
            if block["parent"]:
                lines.append(block["parent"])
            lines += block["commands"]
            lines.append("end")
        return lines

    # ---- tests ----------------------------------------------------------------------
    def test_multi_block_change_in_exact_order(self):
        blocks = self.MULTI
        result = self.precheck(blocks, verification=["show vlan"])
        self.assertEqual(result["status"], "pending_approval", result.get("rejection_reasons"))
        self.assertNotIn(self.FAKE.configure_commands[0], self.device.log)  # precheck is read-only
        self.assertEqual([b["status"] for b in result["dry_run"]["blocks"]], ["PASS"] * len(blocks))
        self.assertEqual(result["dry_run"]["checks"]["device_connectivity"], "PASS")
        self.backup.assert_called_once()

        outcome = self.apply()
        writes = self.device.config_writes()
        self.assertEqual(writes[:len(self.expected_writes(blocks))], self.expected_writes(blocks))
        self.assertEqual(outcome["status"], "applied")
        final = self.results[-1]
        self.assertEqual((final["outcome"], final["execution"]), ("applied", "success"))
        self.assertEqual([b["status"] for b in final["blocks"]], ["applied"] * len(blocks))
        self.assertEqual(final["verification"][0]["command"], "show vlan")
        self.assertTrue(final["verification"][0]["ok"])
        self.applied.assert_called_once_with("a1")
        self.job_ok.assert_called_once_with("job-1")
        self.assertEqual(self.events().count("apply_block_completed"), len(blocks))
        self.assertIn("apply_completed", self.events())
        self.check_state_after_multi()

    def test_failure_in_middle_block_is_reported_as_partial_apply(self):
        blocks = self.MULTI
        self.precheck(blocks)
        bad = blocks[1]["commands"][-1]
        self.device.reject = {bad}
        with self.assertRaisesRegex(self.workflow.ChangeError, "PARTIAL APPLY"):
            self.apply()
        final = self.results[-1]
        self.assertEqual(final["execution"], "partial")
        self.assertEqual([b["status"] for b in final["blocks"]],
                         ["applied", "failed"] + ["not_attempted"] * (len(blocks) - 2))
        self.assertEqual(final["blocks"][1]["failed_command"], len(blocks[1]["commands"]))
        self.assertTrue(final["blocks"][1]["error"].startswith("%"), final["blocks"][1]["error"])
        # Nothing after the rejected command reached the device (except leaving config mode).
        after = self.device.log[self.device.log.index(bad) + 1:]
        for later in blocks[2:]:
            for command in later["commands"]:
                self.assertNotIn(command, after)
        self.failed.assert_called_once_with("a1")
        self.job_failed.assert_called_once()
        self.assertIn("apply_block_failed", self.events())
        self.assertIn("apply_block_not_attempted", self.events())

    def test_failure_in_first_block_applies_nothing(self):
        self.precheck(self.MULTI)
        self.device.reject = {self.MULTI[0]["commands"][0]}
        with self.assertRaisesRegex(self.workflow.ChangeError, "no block was applied"):
            self.apply()
        self.assertEqual([b["status"] for b in self.results[-1]["blocks"]],
                         ["failed"] + ["not_attempted"] * (len(self.MULTI) - 1))

    def test_postcheck_mismatch_for_verifiable_command(self):
        blocks = [{"parent": self.IFACE, "commands": ["description POSTCHECK-TEST"]}]
        self.precheck(blocks)
        self.device.ignore_writes = True
        with self.assertRaisesRegex(self.workflow.ChangeError, "Post-check failed .*description POSTCHECK-TEST"):
            self.apply()
        self.assertEqual(self.results[-1]["execution"], "success")  # the device accepted it ...
        self.assertEqual(self.results[-1]["semantic"], "failed")    # ... but the state is not there

    def test_already_in_effect_needs_no_change(self):
        blocks = [{"parent": self.IFACE, "commands": [self.PRESENT]}]
        result = self.precheck(blocks)
        self.assertEqual(result["status"], "no_change_required")
        self.backup.assert_not_called()

    def test_legacy_single_parent_request(self):
        result = self.workflow.run_precheck(self.HOST, ["description LEGACY"], [self.IFACE], backup=self.backup)
        self.assertEqual(result["status"], "pending_approval")
        self.assertEqual(self.create_approval.call_args.kwargs["config_blocks"],
                         [{"order": 0, "parent": self.IFACE, "commands": ["description LEGACY"]}])
        self.apply()
        self.assertIn("description LEGACY", self.device.log)

    def test_credentials_never_in_output(self):
        self.precheck([{"parent": self.IFACE, "commands": ["description X1"]}])
        self.apply()
        self.assertNotIn(TEST_PASSWORD, repr(self.results))


@unittest.skipUnless(HAVE_ANSIBLE, "needs ansible-playbook and the dellemc collections (worker image)")
class SimulatedOs10Tests(SimulatedFlow, unittest.TestCase):
    FAKE, PLATFORM, HOST = FakeOS10, "dell_os10", "Kenda-Core-1"
    IFACE = "interface ethernet1/1/18"
    PRESENT = "switchport access vlan 20"
    MULTI = [
        {"parent": "interface ethernet1/1/18", "commands": ["description APP-SERVER", "switchport mode trunk",
                                                            "switchport trunk allowed vlan 10,20,30"]},
        {"parent": "interface ethernet1/1/19", "commands": ["description USER-PC", "switchport mode access",
                                                            "switchport access vlan 40"]},
        {"parent": "interface vlan50", "commands": ["description USERS"]},
        {"parent": "router ospf 1", "commands": ["router-id 192.0.2.1"]},
        {"parent": None, "commands": ["ip domain-name lab.example", "spanning-tree mode rstp"]},
    ]

    def check_state_after_multi(self):
        e18, e19 = self.device.interfaces["ethernet1/1/18"], self.device.interfaces["ethernet1/1/19"]
        self.assertEqual((e18["description"], e18["mode"], e18["allowed"]), ("APP-SERVER", "trunk", {10, 20, 30}))
        self.assertEqual((e19["mode"], e19["access_vlan"], e19["allowed"]), ("access", 40, set()))
        self.assertIn(50, self.device.vlans)
        self.assertEqual(self.device.contexts["router ospf 1"], ["router-id 192.0.2.1"])
        self.assertEqual(self.device.global_lines, ["ip domain-name lab.example", "spanning-tree mode rstp"])
        semantic = {c["command"]: c["status"] for b in self.results[-1]["post_check"]["blocks"] for c in b["commands"]}
        self.assertEqual(semantic["switchport trunk allowed vlan 10,20,30"], "verified")
        self.assertEqual(semantic["router-id 192.0.2.1"], "verified")

    def test_missing_physical_interface_fails_precheck(self):
        result = self.precheck([{"parent": "interface ethernet1/1/18", "commands": ["description OK"]},
                                {"parent": "interface ethernet1/1/99", "commands": ["description NOPE"]}])
        self.assertEqual(result["status"], "precheck_failed")
        self.assertEqual([b["status"] for b in result["dry_run"]["blocks"]], ["PASS", "FAILED"])
        self.backup.assert_not_called()
        self.create_approval.assert_not_called()


@unittest.skipUnless(HAVE_ANSIBLE, "needs ansible-playbook and the dellemc collections (worker image)")
class SimulatedOs6Tests(SimulatedFlow, unittest.TestCase):
    FAKE, PLATFORM, HOST = FakeOS6, "dell_os6", "Kenda-HARO-IDF-A"
    IFACE = "interface Tw1/0/3"
    PRESENT = "description PRECHECK-TEST"
    MULTI = [
        {"parent": "interface Tw1/0/3", "commands": ["description APP-SERVER", "switchport mode trunk"]},
        {"parent": "interface Tw1/0/4", "commands": ["description USER-PC", "switchport access vlan 20"]},
        {"parent": "vlan 50", "commands": ["name USERS"]},
        {"parent": None, "commands": ["ip routing", "spanning-tree mode rstp"]},
    ]

    def check_state_after_multi(self):
        t3, t4 = self.device.interfaces["Tw1/0/3"], self.device.interfaces["Tw1/0/4"]
        self.assertEqual((t3["description"], t3["extra"]), ("APP-SERVER", ["switchport access vlan 10", "switchport mode trunk"]))
        self.assertEqual((t4["description"], t4["extra"]), ("USER-PC", ["switchport access vlan 20"]))
        self.assertIn(50, self.device.vlans)
        self.assertEqual(self.device.contexts["vlan 50"], ["name USERS"])
        self.assertEqual(self.device.global_lines, ["ip routing", "spanning-tree mode rstp"])


if __name__ == "__main__":
    unittest.main()
