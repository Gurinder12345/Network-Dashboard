"""
Shared change workflow (Dell OS6 + Dell OS10): OS10 policy, OS10 verifier, platform
dispatch, precheck and apply lifecycles, sanitized errors, and OS6 regression. DB, Vault,
devices and Ansible are mocked here (no switch is ever contacted). Real Netmiko/Ansible
against a simulated OS10 CLI is in test_os10_simulated.py.
    python -m unittest tests.test_change_workflow -v
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from changes import os10, platforms, workflow  # noqa: E402
from changes.policy import PolicyViolation, check_os10  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "os10")


def fixture(name):
    with open(os.path.join(FIX, name)) as handle:
        return handle.read()


ABSENT = fixture("running_config_description_absent.txt")
PRESENT = fixture("running_config_description_present.txt")
PARENT = ["interface ethernet1/1/5"]
DESC = ["description AUTOMATION-TEST"]
OS10_DEVICE = {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "192.0.2.10", "platform": "dell_os10", "credential_path": "x"}
OS6_DEVICE = {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "192.0.2.31", "platform": "dell_os6", "credential_path": "x"}
BACKUP_JOB = "bbbbbbbb-0000-0000-0000-000000000001"
APPROVAL = "aaaaaaaa-0000-0000-0000-000000000001"


class PolicyTests(unittest.TestCase):
    def test_allowed_and_canonicalized(self):
        self.assertEqual(check_os10(["description AUTOMATION-TEST"], ["interface ethernet 1/1/5"]),
                         (["description AUTOMATION-TEST"], ["interface ethernet1/1/5"]))
        self.assertEqual(check_os10(['description "LAB-01"'], ["interface Ethernet1/1/26:2"]),
                         (["description LAB-01"], ["interface ethernet1/1/26:2"]))
        self.assertEqual(check_os10(["no description"], PARENT), (["no description"], PARENT))

    def test_dangerous_commands_are_rejected_with_reasons(self):
        cases = {
            "reload": "system", "reboot": "system",
            "write memory": "system", "delete startup-configuration": "system", "restore factory-defaults": "system",
            "username x password y role sysadmin": "credentials", "aaa authentication login default local": "aaa",
            "tacacs-server host 192.0.2.5": "aaa", "radius-server host 192.0.2.6": "aaa", "ip ssh server enable": "ssh",
            "ip route 0.0.0.0/0 192.0.2.1": "routing", "no router bgp 65000": "routing", "router ospf 1": "routing",
            "no vlan 10": "vlan", "no switchport": "switchport", "switchport mode hybrid": "switchport",
            "channel-group 10 mode active": "port_channel", "spanning-tree mode rstp": "spanning_tree",
            "ip address 192.0.2.9/24": "ip_address", "ip vrf forwarding management": "management",
            "mtu 9216": "mtu", "description has spaces": "description",
        }
        for command, category in cases.items():
            with self.subTest(command=command):
                with self.assertRaises(PolicyViolation) as ctx:
                    check_os10([command], PARENT)
                self.assertEqual(ctx.exception.violations[0]["category"], category)

    def test_parents(self):
        for parents, category in ((["interface mgmt1/1/1"], "management"), (["interface port-channel10"], "port_channel"),
                                  (["interface vlan10"], "vlan"), ([], "context"),
                                  (["interface ethernet1/1/5", "interface ethernet1/1/6"], "context"),
                                  (["router bgp 65000"], "routing")):
            with self.subTest(parents=parents):
                with self.assertRaises(PolicyViolation) as ctx:
                    check_os10(DESC, parents)
                self.assertIn(category, [v["category"] for v in ctx.exception.violations])

    def test_one_description_change_per_request(self):
        with self.assertRaises(PolicyViolation):
            check_os10(["description A", "description B"], PARENT)
        with self.assertRaises(PolicyViolation):
            check_os10(["description A", "no description"], PARENT)
        with self.assertRaises(PolicyViolation):
            check_os10([], PARENT)

    def test_policy_never_hard_codes_an_interface(self):
        for port in ("1/1/1", "1/1/17", "1/1/32", "1/1/26:4"):
            self.assertEqual(check_os10(DESC, [f"interface ethernet{port}"])[1], [f"interface ethernet{port}"])


class VerifierTests(unittest.TestCase):
    def test_absent_present_and_idempotent(self):
        absent = os10.verify(DESC, PARENT, ABSENT)
        self.assertTrue(absent["would_change"])
        self.assertEqual(absent["command_results"][0]["current_value"], None)
        present = os10.verify(DESC, PARENT, PRESENT)
        self.assertFalse(present["would_change"])
        self.assertEqual(present["already_present"], DESC)

    def test_no_description(self):
        self.assertFalse(os10.verify(["no description"], PARENT, ABSENT)["would_change"])
        self.assertTrue(os10.verify(["no description"], PARENT, PRESENT)["would_change"])

    def test_different_existing_description_is_a_change(self):
        cfg = PRESENT.replace("description AUTOMATION-TEST", "description SOMETHING-ELSE")
        self.assertTrue(os10.verify(DESC, PARENT, cfg)["would_change"])

    def test_block_boundaries_and_other_interfaces(self):
        # The description of ethernet1/1/30:2 must not leak into ethernet1/1/5's block.
        self.assertTrue(os10.verify(["description UPLINK-DO-NOT-TOUCH"], PARENT, ABSENT)["would_change"])
        self.assertFalse(os10.verify(["description UPLINK-DO-NOT-TOUCH"], ["interface ethernet1/1/30:2"], ABSENT)["would_change"])

    def test_missing_interface_and_unrecognised_output(self):
        with self.assertRaisesRegex(os10.VerificationError, "not found"):
            os10.verify(DESC, ["interface ethernet1/1/99"], ABSENT)
        with self.assertRaisesRegex(os10.VerificationError, "not recognisable"):
            os10.verify(DESC, PARENT, "% Error: Unrecognized command.")


class DispatchTests(unittest.TestCase):
    def test_platforms(self):
        self.assertIsInstance(platforms.platform_for("dell_os6"), platforms.Os6Platform)
        self.assertIsInstance(platforms.platform_for("dell_os10"), platforms.Os10Platform)
        with self.assertRaises(platforms.UnsupportedPlatform):
            platforms.platform_for("cisco_ios")
        self.assertFalse(platforms.platform_for("dell_os6").pre_apply_verify)
        self.assertTrue(platforms.platform_for("dell_os10").pre_apply_verify)

    def test_os6_prepare_is_a_pass_through(self):
        lines, parents = platforms.platform_for("dell_os6").prepare(["shutdown"], ["interface Tw1/0/3"])
        self.assertEqual((lines, parents), (["shutdown"], ["interface Tw1/0/3"]))


class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        p = lambda name, **kw: mock.patch.object(workflow, name, **kw).start()
        self.device_by_host = p("get_device_by_hostname", return_value=dict(OS10_DEVICE))
        self.device_by_id = p("get_device_by_id", return_value=dict(OS10_DEVICE))
        self.create_approval = p("create_change_approval", return_value={"approval_id": APPROVAL, "status": "pending"})
        self.audit = p("create_audit_event")
        self.get_approval = p("get_change_approval")
        self.verify_backup = p("verify_backup_for_device", return_value={"valid": True})
        self.applying = p("mark_approval_applying")
        self.applied = p("mark_approval_applied")
        self.failed = p("mark_approval_failed")
        self.backup = mock.Mock(return_value={"status": "success", "job_id": BACKUP_JOB})
        os10_cls, os6_cls = platforms.Os10Platform, platforms.Os6Platform
        self.read = mock.patch.object(os10_cls, "read_running_config", return_value=ABSENT).start()
        self.write = mock.patch.object(os10_cls, "apply", return_value={"returncode": 0, "stdout": "ok", "stderr": ""}).start()
        self.os6_read = mock.patch.object(os6_cls, "read_running_config", return_value="!os6").start()
        self.os6_verify = mock.patch.object(os6_cls, "verify", return_value={"would_change": True, "proposed_changes": ["vlan 200"],
                                                                             "already_present": [], "verification_method": "show vlan"}).start()
        self.os6_write = mock.patch.object(os6_cls, "apply", return_value={"returncode": 0, "stdout": "", "stderr": ""}).start()

    def events(self):
        return [c.kwargs["event_type"] for c in self.audit.call_args_list]

    def approval(self, status="applying", platform_device=OS10_DEVICE, lines=DESC, parents=PARENT):
        self.device_by_id.return_value = dict(platform_device)
        self.get_approval.return_value = {"approval_id": APPROVAL, "device_id": platform_device["id"], "backup_job_id": BACKUP_JOB,
                                          "status": status, "approved_by": "approver", "config_lines": lines, "config_parents": parents}


class PrecheckTests(WorkflowTestCase):
    def test_os10_precheck_success_backs_up_the_same_snapshot(self):
        result = workflow.run_precheck("Kenda-Core-1", ["description AUTOMATION-TEST"], ["interface ethernet 1/1/5"], backup=self.backup)
        self.assertEqual((result["status"], result["platform"], result["ready_for_approval"]), ("pending_approval", "dell_os10", True))
        self.read.assert_called_once_with("Kenda-Core-1")
        self.backup.assert_called_once_with("Kenda-Core-1", config_snapshot=ABSENT)
        kwargs = self.create_approval.call_args.kwargs
        self.assertEqual((kwargs["config_lines"], kwargs["config_parents"], kwargs["backup_job_id"]), (DESC, PARENT, BACKUP_JOB))
        self.assertEqual(self.events(), ["precheck_completed"])
        self.write.assert_not_called()  # precheck is read-only

    def test_already_present_needs_no_backup_or_approval(self):
        self.read.return_value = PRESENT
        result = workflow.run_precheck("Kenda-Core-1", DESC, PARENT, backup=self.backup)
        self.assertEqual(result["status"], "no_change_required")
        self.backup.assert_not_called()
        self.create_approval.assert_not_called()

    def test_policy_rejection_is_reported_before_any_device_access(self):
        with self.assertRaises(PolicyViolation):
            workflow.run_precheck("Kenda-Core-1", ["reload"], PARENT, backup=self.backup)
        self.read.assert_not_called()
        self.assertEqual(self.events(), ["precheck_failed"])

    def test_read_failures_are_sanitized(self):
        cases = (
            (RuntimeError("TCP connection to device failed. 192.0.2.10 password=hunter2"), "Device unreachable"),
            (RuntimeError("NetmikoAuthenticationException: Authentication to device failed."), "SSH authentication failed"),
            (type("Forbidden", (Exception,), {"__module__": "hvac.exceptions"})("permission denied"), "Credential lookup failed"),
        )
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.read.side_effect = exc
                with self.assertRaises(workflow.ChangeError) as ctx:
                    workflow.run_precheck("Kenda-Core-1", DESC, PARENT, backup=self.backup)
                self.assertEqual(str(ctx.exception), f"Could not read the running configuration: {expected}")
                self.assertNotIn("hunter2", str(ctx.exception))
        self.backup.assert_not_called()

    def test_backup_failure_prevents_approval(self):
        self.backup.return_value = {"status": "failed"}
        with self.assertRaisesRegex(RuntimeError, "Backup failed"):
            workflow.run_precheck("Kenda-Core-1", DESC, PARENT, backup=self.backup)
        self.create_approval.assert_not_called()

    def test_disabled_or_unknown_device_and_unsupported_platform(self):
        self.device_by_host.side_effect = ValueError("Enabled device not found in database: X")
        with self.assertRaises(ValueError):
            workflow.run_precheck("X", DESC, PARENT, backup=self.backup)
        self.device_by_host.side_effect = None
        self.device_by_host.return_value = {**OS10_DEVICE, "platform": "cisco_ios"}
        with self.assertRaises(platforms.UnsupportedPlatform):
            workflow.run_precheck("Kenda-Core-1", DESC, PARENT, backup=self.backup)

    def test_legacy_os6_task_refuses_os10_devices(self):
        with self.assertRaisesRegex(ValueError, "not dell_os6"):
            workflow.run_precheck("Kenda-Core-1", DESC, PARENT, backup=self.backup, expected_platform="dell_os6")


class ApplyTests(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        self.read.side_effect = [ABSENT, PRESENT]  # pre-apply read, then post-check read

    def test_applied_only_after_postcheck(self):
        self.approval()
        order = []
        self.write.side_effect = lambda *a: order.append("write") or {"returncode": 0, "stdout": "", "stderr": ""}
        self.applied.side_effect = lambda *a: order.append("applied")
        original = self.read.side_effect
        self.read.side_effect = lambda host: (order.append("read"), original.pop(0))[1]
        original = [ABSENT, PRESENT]
        result = workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.assertEqual(order, ["read", "write", "read", "applied"])
        self.assertEqual((result["status"], result["write_skipped"]), ("applied", False))
        self.write.assert_called_once_with("Kenda-Core-1", DESC, PARENT)
        self.assertEqual(self.events(), ["apply_started", "apply_completed"])
        self.applying.assert_not_called()  # the API already claimed it

    def test_ansible_failure_marks_failed_with_sanitized_error(self):
        self.approval()
        self.write.return_value = {"returncode": 2, "stdout": fixture("ansible_apply_device_error.txt"), "stderr": ""}
        with self.assertRaises(workflow.ChangeError) as ctx:
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.assertEqual(str(ctx.exception), "Ansible apply failed (rc=2): % Error: Command rejected by simulated device. "
                                             "(command: description AUTOMATION-TEST)")
        self.failed.assert_called_once_with(APPROVAL)
        self.applied.assert_not_called()
        self.assertEqual(self.events(), ["apply_started", "apply_failed"])

    def test_postcheck_mismatch_fails_even_though_ansible_succeeded(self):
        self.approval()
        self.read.side_effect = [ABSENT, ABSENT]
        with self.assertRaisesRegex(workflow.ChangeError, r"Post-check failed .*\(missing: description AUTOMATION-TEST\)"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.failed.assert_called_once()
        self.applied.assert_not_called()

    def test_postcheck_read_failure_is_failed_and_says_verify_manually(self):
        self.approval()
        self.read.side_effect = [ABSENT, RuntimeError("Unable to connect to port 22")]
        with self.assertRaisesRegex(workflow.ChangeError, "may have been applied - verify manually: Device unreachable"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.failed.assert_called_once()

    def test_idempotent_when_already_in_desired_state(self):
        self.approval()
        self.read.side_effect = [PRESENT]
        result = workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.assertTrue(result["write_skipped"])
        self.write.assert_not_called()
        self.applied.assert_called_once_with(APPROVAL)
        self.assertIn("no commands sent", self.audit.call_args.kwargs["message"])

    def test_backup_must_be_valid_before_any_write(self):
        self.approval()
        self.verify_backup.return_value = {"valid": False, "reason": "Backup job status is failed"}
        with self.assertRaisesRegex(ValueError, "Backup verification failed"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.read.assert_not_called()
        self.write.assert_not_called()
        self.failed.assert_called_once_with(APPROVAL)
        self.assertEqual(self.events(), ["apply_failed"])

    def test_wrong_states_never_reach_the_device(self):
        for status in ("pending", "approved", "cancelled", "applied", "failed"):
            with self.subTest(status=status):
                self.approval(status=status)
                with self.assertRaisesRegex(ValueError, f"is {status}, expected applying"):
                    workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.write.assert_not_called()
        self.failed.assert_not_called()  # status untouched: it is not this task's claim

    def test_tampered_stored_change_is_rejected_by_policy_at_apply(self):
        self.approval(lines=["no switchport"])
        with self.assertRaises(PolicyViolation):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.write.assert_not_called()
        self.failed.assert_called_once()


class Os6RegressionTests(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        self.device_by_host.return_value = dict(OS6_DEVICE)

    def test_os6_precheck_unchanged(self):
        result = workflow.run_precheck("Kenda-HARO-IDF-A", ["shutdown"], ["interface Tw1/0/3"], backup=self.backup,
                                       expected_platform="dell_os6")
        self.assertEqual(result["status"], "pending_approval")
        self.os6_verify.assert_called_once_with("Kenda-HARO-IDF-A", ["shutdown"], ["interface Tw1/0/3"], "!os6")
        self.backup.assert_called_once_with("Kenda-HARO-IDF-A", config_snapshot="!os6")
        self.assertEqual(self.create_approval.call_args.kwargs["config_lines"], ["shutdown"])  # no OS10 policy on OS6

    def test_os6_apply_has_no_pre_apply_read_and_requires_postcheck(self):
        self.approval(platform_device=OS6_DEVICE, lines=["vlan 200"], parents=None)
        self.os6_verify.return_value = {"would_change": False, "proposed_changes": [], "already_present": ["vlan 200"],
                                        "verification_method": "show vlan"}
        result = workflow.run_apply(APPROVAL, claimed_by_api=True, expected_platform="dell_os6")
        self.assertEqual(result["status"], "applied")
        self.os6_write.assert_called_once_with("Kenda-HARO-IDF-A", ["vlan 200"], None)
        self.assertEqual(self.os6_verify.call_count, 1)  # post-check only
        self.applied.assert_called_once()

    def test_os6_postcheck_failure_and_platform_guard(self):
        self.approval(platform_device=OS6_DEVICE, lines=["vlan 200"], parents=None)
        with self.assertRaisesRegex(workflow.ChangeError, "Post-check failed"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.failed.assert_called_once()
        self.failed.reset_mock()
        self.approval()  # an OS10 approval through the legacy OS6 task
        with self.assertRaisesRegex(ValueError, "not a dell_os6 device"):
            workflow.run_apply(APPROVAL, claimed_by_api=True, expected_platform="dell_os6")
        self.failed.assert_called_once()
        self.write.assert_not_called()

    def test_direct_os6_apply_claims_approved(self):
        self.approval(status="approved", platform_device=OS6_DEVICE, lines=["vlan 200"], parents=None)
        self.os6_verify.return_value = {"would_change": False, "proposed_changes": [], "already_present": ["vlan 200"],
                                        "verification_method": "show vlan"}
        workflow.run_apply(APPROVAL, claimed_by_api=False)
        self.applying.assert_called_once_with(APPROVAL)


class AnsibleSummaryTests(unittest.TestCase):
    def test_success_fixture_parses_and_failure_is_short(self):
        self.assertIn("ok=", fixture("ansible_apply_success.txt"))
        summary = workflow.ansible_failure_summary({"returncode": 2, "stdout": fixture("ansible_apply_device_error.txt")})
        self.assertLess(len(summary), 200)
        self.assertNotIn("\\r", summary)

    def test_timeout_and_unknown(self):
        self.assertEqual(workflow.ansible_failure_summary({"returncode": -1, "stdout": "", "stderr": "ansible-playbook timed out after 180 s"}),
                         "Ansible apply failed (rc=-1): ansible-playbook timed out after 180 s")


class WorkerTaskRegistrationTests(unittest.TestCase):
    def test_generic_and_legacy_tasks(self):
        import worker

        for name in ("network_worker.change_precheck", "network_worker.apply_approved_change",
                     "network_worker.os6_change_precheck", "network_worker.os6_apply_approved_change"):
            self.assertIn(name, worker.app.tasks)
        with mock.patch.object(worker, "run_change_apply") as run:
            worker.os6_apply_approved_change("x", claimed_by_api=True)
            run.assert_called_once_with("x", claimed_by_api=True, expected_platform="dell_os6")
        with mock.patch.object(worker, "run_change_precheck") as run:
            worker.change_precheck("Kenda-Core-1", DESC, PARENT)
            self.assertIs(run.call_args.kwargs["backup"], worker.backup_running_config_task)
            self.assertNotIn("expected_platform", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
