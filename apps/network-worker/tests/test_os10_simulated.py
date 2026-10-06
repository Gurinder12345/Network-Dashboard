"""
Dell OS10 change path with the REAL Netmiko reader and the REAL Ansible playbook
(dellemc.os10 connection plugins + ansible.netcommon.cli_command) against a simulated OS10
CLI on 127.0.0.1 (tests/fakes/os10_device.py). No switch is contacted. Needs the worker
image (ansible-playbook + collections). DB/audit calls are mocked.
    python -m unittest tests.test_os10_simulated -v
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

from fakes.os10_device import TEST_PASSWORD, TEST_USERNAME, FakeOS10  # noqa: E402

HAVE_ANSIBLE = shutil.which("ansible-playbook") is not None and os.path.isdir(
    "/usr/share/ansible/collections/ansible_collections/dellemc/os10")

DESC = ["description AUTOMATION-TEST"]
PARENT = ["interface ethernet1/1/5"]


@unittest.skipUnless(HAVE_ANSIBLE, "needs ansible-playbook and the dellemc.os10 collection (worker image)")
class SimulatedOs10Tests(unittest.TestCase):
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
        import ansible.run_os10 as r10
        import tasks.dell_os10 as d10
        from changes import platforms, workflow

        self.addCleanup(mock.patch.stopall)
        self.workflow, self.platform = workflow, platforms.platform_for("dell_os10")
        self.device = FakeOS10()
        self.addCleanup(self.device.close)
        self.inventory(self.device.port)
        creds = {"username": TEST_USERNAME, "password": TEST_PASSWORD, "secret": ""}
        self.creds = creds
        mock.patch.object(d10, "INVENTORY_DIR", self.inv).start()
        mock.patch.object(d10, "get_device_credentials", side_effect=lambda path: dict(self.creds)).start()
        mock.patch.object(r10, "HOSTS_FILE", os.path.join(self.inv, "hosts.yaml")).start()
        mock.patch.object(r10, "get_device_credentials", side_effect=lambda path: dict(self.creds)).start()

    def inventory(self, port):
        self.inv = tempfile.mkdtemp(dir=self.tmp)
        host = {"Kenda-Core-1": {"hostname": "127.0.0.1", "port": port, "platform": "dell_os10", "groups": ["dell_os10"],
                                 "data": {"credential_path": "test/simulated"}}}
        with open(os.path.join(self.inv, "hosts.yaml"), "w") as h:
            yaml.safe_dump(host, h)
        for name, data in (("groups.yaml", {"dell_os10": {}}), ("defaults.yaml", {})):
            with open(os.path.join(self.inv, name), "w") as h:
                yaml.safe_dump(data, h)

    def config_writes(self):
        start = self.device.log.index("configure terminal") if "configure terminal" in self.device.log else None
        return self.device.log[start:] if start is not None else []

    def test_read_and_verify_with_netmiko(self):
        config = self.platform.read_running_config("Kenda-Core-1")
        self.assertIn("interface ethernet1/1/5", config)
        result = self.platform.verify("Kenda-Core-1", DESC, PARENT, config)
        self.assertEqual(result["proposed_changes"], DESC)
        self.assertNotIn("configure terminal", self.device.log)  # reads never enter config mode

    def test_ansible_apply_sends_exactly_the_approved_commands(self):
        result = self.platform.apply("Kenda-Core-1", DESC, PARENT)
        self.assertEqual(result["returncode"], 0, result["stdout"][-800:])
        self.assertEqual(self.config_writes(), ["configure terminal", "interface ethernet1/1/5", "description AUTOMATION-TEST", "end"])
        self.assertEqual(self.device.interfaces["ethernet1/1/5"]["description"], "AUTOMATION-TEST")
        self.assertNotIn(TEST_PASSWORD, result["stdout"] + result["stderr"])

    def test_ansible_reports_device_rejection(self):
        self.device.reject = {"description AUTOMATION-TEST"}
        result = self.platform.apply("Kenda-Core-1", DESC, PARENT)
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("% Error: Command rejected by simulated device.", self.workflow.ansible_failure_summary(result))

    def approval_flow(self):
        p = lambda name, **kw: mock.patch.object(self.workflow, name, **kw).start()
        p("get_change_approval", return_value={"approval_id": "a1", "device_id": 12, "backup_job_id": "b1", "status": "applying",
                                               "approved_by": "approver", "config_lines": DESC, "config_parents": PARENT})
        p("get_device_by_id", return_value={"id": 12, "hostname": "Kenda-Core-1", "platform": "dell_os10"})
        p("verify_backup_for_device", return_value={"valid": True})
        p("create_audit_event")
        return p("mark_approval_applied"), p("mark_approval_failed")

    def test_full_apply_with_postcheck(self):
        applied, failed = self.approval_flow()
        result = self.workflow.run_apply("a1", claimed_by_api=True)
        self.assertEqual((result["status"], result["write_skipped"]), ("applied", False))
        self.assertEqual(result["post_check"]["already_present"], DESC)
        applied.assert_called_once_with("a1")
        failed.assert_not_called()

    def test_postcheck_mismatch_on_device_that_ignores_writes(self):
        self.device.ignore_writes = True
        applied, failed = self.approval_flow()
        with self.assertRaisesRegex(self.workflow.ChangeError, "Post-check failed"):
            self.workflow.run_apply("a1", claimed_by_api=True)
        failed.assert_called_once_with("a1")
        applied.assert_not_called()

    def test_already_present_sends_nothing(self):
        self.device.interfaces["ethernet1/1/5"]["description"] = "AUTOMATION-TEST"
        applied, _ = self.approval_flow()
        result = self.workflow.run_apply("a1", claimed_by_api=True)
        self.assertTrue(result["write_skipped"])
        self.assertNotIn("configure terminal", self.device.log)
        applied.assert_called_once()

    def test_auth_failure_and_unreachable_are_sanitized(self):
        self.creds["password"] = "wrong-test-password"
        with self.assertRaisesRegex(self.workflow.ChangeError, "^Could not read the running configuration: SSH authentication failed$"):
            self._precheck()
        self.creds["password"] = TEST_PASSWORD
        self.device.close()
        closed = FakeOS10()
        port = closed.port
        closed.close()
        self.inventory(port)
        import tasks.dell_os10 as d10
        mock.patch.object(d10, "INVENTORY_DIR", self.inv).start()
        with self.assertRaisesRegex(self.workflow.ChangeError, "Device unreachable"):
            self._precheck()

    def _precheck(self):
        p = lambda name, **kw: mock.patch.object(self.workflow, name, **kw).start()
        p("get_device_by_hostname", return_value={"id": 12, "hostname": "Kenda-Core-1", "platform": "dell_os10"})
        p("create_audit_event")
        return self.workflow.run_precheck("Kenda-Core-1", DESC, PARENT, backup=mock.Mock())


    # ---- safe-L2 (V1.1) -------------------------------------------------------------------
    def l2_flow(self, lines, interface):
        """Real precheck (Netmiko) -> stored device commands -> real apply (Ansible) -> post-check."""
        p = lambda name, **kw: mock.patch.object(self.workflow, name, **kw).start()
        device = {"id": 12, "hostname": "Kenda-Core-1", "platform": "dell_os10", "management_ip": "192.0.2.10"}
        p("get_device_by_hostname", return_value=device)
        p("get_device_by_id", return_value=device)
        p("create_audit_event")
        p("verify_backup_for_device", return_value={"valid": True})
        approval = p("create_change_approval", return_value={"approval_id": "a1", "status": "pending"})
        applied, failed = p("mark_approval_applied"), p("mark_approval_failed")
        mock.patch("db.topology.list_inventory", return_value=[
            {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "192.0.2.10", "platform": "dell_os10"},
            {"id": 13, "hostname": "Kenda-Core-2", "management_ip": "192.0.2.20", "platform": "dell_os10"}]).start()
        mock.patch("db.topology.list_lldp_identities", return_value=[
            {"device_id": 13, "lldp_system_name": "kenda-core-02", "chassis_id": "e8:b5:d0:7a:5c:a3"}]).start()
        mock.patch("db.topology.list_links_for_interface", return_value=[]).start()
        backup = mock.Mock(return_value={"status": "success", "job_id": "b1"})

        result = self.workflow.run_precheck("Kenda-Core-1", lines, [f"interface {interface}"], backup=backup)
        if result["status"] != "pending_approval":
            return result, None
        self.assertNotIn("configure terminal", self.device.log)  # precheck is read-only
        stored = approval.call_args.kwargs
        p("get_change_approval", return_value={"approval_id": "a1", "device_id": 12, "backup_job_id": "b1",
                                               "status": "applying", "approved_by": "approver",
                                               "config_lines": stored["config_lines"],
                                               "config_parents": stored["config_parents"]})
        outcome = self.workflow.run_apply("a1", claimed_by_api=True)
        applied.assert_called_once_with("a1")
        failed.assert_not_called()
        return result, outcome

    def test_l2_read_is_one_session_and_read_only(self):
        state = self.platform.read_device_state("Kenda-Core-1", ["switchport access vlan 30"])
        self.assertIn("interface ethernet1/1/18", state["running_config"])
        self.assertIn("kenda-core-02", state["lldp"])
        self.assertIsNone(state["lldp_error"])
        self.assertEqual(self.device.connections, 1)
        self.assertEqual([c for c in self.device.log if not c.startswith("terminal")],
                         ["show running-configuration", "show lldp neighbors"])
        description_only = self.platform.read_device_state("Kenda-Core-1", ["description X"])
        self.assertIsNone(description_only["lldp"])  # V1 path unchanged: no LLDP read

    def test_trunk_conversion_with_real_reader_and_ansible(self):
        result, outcome = self.l2_flow(["switchport mode trunk", "switchport trunk allowed vlan 30,40"], "ethernet1/1/18")
        self.assertEqual(outcome["sent_lines"], ["switchport mode trunk", "switchport trunk allowed vlan 30,40"])
        self.assertEqual(self.config_writes()[:5], ["configure terminal", "interface ethernet1/1/18", "switchport mode trunk",
                                                    "switchport trunk allowed vlan 30,40", "end"])
        port = self.device.interfaces["ethernet1/1/18"]
        self.assertEqual((port["mode"], port["allowed"], port["access_vlan"]), ("trunk", {30, 40}, 20))

    def test_allowed_vlan_set_and_shutdown_with_real_ansible(self):
        _, outcome = self.l2_flow(["switchport trunk allowed vlan 20,30"], "ethernet1/1/19")
        self.assertEqual(self.device.interfaces["ethernet1/1/19"]["allowed"], {20, 30})
        self.assertEqual(outcome["post_check"]["already_present"],
                         ["switchport trunk allowed vlan 20,30", "no switchport trunk allowed vlan 10"])
        mock.patch.stopall()
        self.setUp()
        self.l2_flow(["shutdown"], "ethernet1/1/18")
        self.assertEqual(self.device.interfaces["ethernet1/1/18"]["admin"], "down")

    def test_uplink_shutdown_is_rejected_from_real_lldp(self):
        result, _ = self.l2_flow(["shutdown"], "ethernet1/1/25")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("is classified as an uplink (live LLDP: kenda-core-02 ethernet1/1/25 = inventory device Kenda-Core-2)",
                      result["rejection_reasons"][0])
        self.assertNotIn("configure terminal", self.device.log)


if __name__ == "__main__":
    unittest.main()
