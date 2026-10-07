"""
Multi-block change workflow (Dell OS6 + Dell OS10), unit level: block model and integrity
validation, removal of command policy, CLI preview, apply steps, semantic verification,
execution interpretation, redaction, and the precheck / apply state machine with DB,
Vault, devices and Ansible mocked (no switch is contacted). Real Netmiko/Ansible against
simulated CLIs: test_change_simulated.py.
    python -m unittest tests.test_change_workflow -v
"""

import base64
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from ansible.blocks_runner import parse_marker  # noqa: E402
from changes import blocks as B  # noqa: E402
from changes import execution as exe  # noqa: E402
from changes import platforms, verify, workflow  # noqa: E402
from fakes import fake_redis  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "os10")


def fixture(name):
    with open(os.path.join(FIX, name)) as handle:
        return handle.read()


OS10_CONFIG = fixture("running_config_l2.txt")
OS6_CONFIG = "\n".join([
    "!Current Configuration:", "configure", "vlan 10,20", "exit", 'hostname "Kenda-HARO-IDF-A"', "ip routing",
    "interface Tw1/0/3", 'description "PRECHECK-TEST"', "switchport access vlan 10", "exit",
    "interface Tw1/0/12", "shutdown", "exit", "exit"])
OS10_DEVICE = {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "192.0.2.10", "platform": "dell_os10", "credential_path": "x"}
OS6_DEVICE = {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "192.0.2.31", "platform": "dell_os6", "credential_path": "x"}
BACKUP_JOB = "bbbbbbbb-0000-0000-0000-000000000001"
APPROVAL = "aaaaaaaa-0000-0000-0000-000000000001"
MULTI = [
    {"parent": "interface ethernet 1/1/18", "commands": ["description APP-SERVER", "switchport mode trunk",
                                                          "switchport trunk allowed vlan 10,20,30"]},
    {"parent": "interface ethernet1/1/19", "commands": ["description USER-PC", "switchport mode access",
                                                        "switchport access vlan 40"]},
    {"parent": "router ospf 1", "commands": ["router-id 10.0.0.1"]},
    {"parent": None, "commands": ["ip routing"]},
]


def marker(applied, failed=None, error=""):
    data = base64.b64encode(json.dumps({"applied": applied, "failed": failed, "error": error}).encode()).decode()
    return f'ok: [x] => {{\n    "msg": "NMP_APPLY_RESULT:{data}"\n}}'


# ---- block model + integrity validation ---------------------------------------------------
class BlockModelTests(unittest.TestCase):
    def test_order_and_text_are_preserved(self):
        blocks = B.normalize_blocks([{"parent": "  interface ethernet1/1/18 ", "commands": ["  shutdown", "description Z", "description A"]},
                                     {"parent": "", "commands": ["ip routing"]}, {"commands": ["ip routing"]}])
        self.assertEqual(blocks, [
            {"order": 0, "parent": "interface ethernet1/1/18", "commands": ["shutdown", "description Z", "description A"]},
            {"order": 1, "parent": None, "commands": ["ip routing"]},
            {"order": 2, "parent": None, "commands": ["ip routing"]}])  # never reordered or de-duplicated

    def test_client_order_field_is_ignored(self):
        blocks = B.normalize_blocks([{"order": 7, "parent": "vlan 50", "commands": ["name A"]},
                                     {"order": 0, "parent": "vlan 60", "commands": ["name B"]}])
        self.assertEqual([(b["order"], b["parent"]) for b in blocks], [(0, "vlan 50"), (1, "vlan 60")])

    def test_malformed_requests_are_rejected(self):
        cases = {
            "not a list": {"parent": "x"}, "zero blocks": [], "block not object": ["shutdown"],
            "missing commands": [{"parent": "vlan 5"}], "zero commands": [{"parent": "vlan 5", "commands": []}],
            "non-string command": [{"parent": None, "commands": [5]}], "null command": [{"parent": None, "commands": [None]}],
            "non-string parent": [{"parent": 7, "commands": ["x"]}], "null byte": [{"parent": None, "commands": ["descr\x00iption"]}],
            "newline injection": [{"parent": None, "commands": ["description a\nreload"]}],
            "escape sequence": [{"parent": None, "commands": ["\x1b[2Jshow"]}], "tab": [{"parent": None, "commands": ["a\tb"]}],
            "empty command": [{"parent": None, "commands": ["   "]}], "unknown field": [{"parent": None, "commands": ["x"], "run": 1}],
            "configure": [{"parent": None, "commands": ["configure terminal"]}], "end": [{"parent": "vlan 5", "commands": ["end"]}],
            "exit": [{"parent": None, "commands": ["exit"]}], "do": [{"parent": None, "commands": ["do reload"]}],
            "non-ascii": [{"parent": None, "commands": ["description café"]}],
        }
        for name, payload in cases.items():
            with self.subTest(name):
                with self.assertRaises(B.BlockError):
                    B.normalize_blocks(payload)

    def test_resource_limits(self):
        with self.assertRaisesRegex(B.BlockError, "at most 50 blocks"):
            B.normalize_blocks([{"parent": None, "commands": ["x"]}] * 51)
        with self.assertRaisesRegex(B.BlockError, "more than 100 commands"):
            B.normalize_blocks([{"parent": None, "commands": ["x"] * 101}])
        with self.assertRaisesRegex(B.BlockError, "at most 500 commands"):
            B.normalize_blocks([{"parent": None, "commands": ["x"] * 100}] * 6)
        with self.assertRaisesRegex(B.BlockError, "longer than 1000"):
            B.normalize_blocks([{"parent": None, "commands": ["x" * 1001]}])
        with self.assertRaisesRegex(B.BlockError, "parent is longer than 1000"):
            B.normalize_blocks([{"parent": "p" * 1001, "commands": ["x"]}])
        self.assertEqual(len(B.normalize_blocks([{"parent": None, "commands": ["x" * 1000] * 100}] * 5)), 5)

    def test_every_problem_is_reported(self):
        with self.assertRaises(B.BlockError) as ctx:
            B.normalize_blocks([{"parent": None, "commands": []}, {"parent": None, "commands": ["ok", 3, "end"]}])
        self.assertEqual(len(ctx.exception.problems), 3)

    def test_command_policy_is_removed(self):
        """Phase 27: no command category is rejected at the application layer (structure only)."""
        commands = ["switchport mode trunk", "shutdown", "no shutdown", "spanning-tree mode rstp", "router ospf 1",
                    "router bgp 65000", "ip route 0.0.0.0/0 192.0.2.1", "aaa authentication login default local",
                    "snmp-server community lab-test ro", "ip access-list LAB", "access-list 10 permit any",
                    "username netops password x role sysadmin", "management route 0.0.0.0/0 192.0.2.1",
                    "interface mgmt1/1/1", "channel-group 10 mode active", "no switchport", "mtu 9216",
                    "switchport trunk native vlan 10", "vlt-domain 1", "no vlan 20", "reload", "write memory",
                    "default interface ethernet1/1/5", "no interface vlan 50", "qos-map traffic-class x",
                    "service-policy input QOS", "ip ssh server enable", "tacacs-server host 192.0.2.5"]
        blocks = B.normalize_blocks([{"parent": None, "commands": commands},
                                     {"parent": "interface mgmt1/1/1", "commands": ["ip address 192.0.2.99/24"]},
                                     {"parent": "router bgp 65000", "commands": ["neighbor 192.0.2.2 remote-as 65001"]}])
        self.assertEqual(blocks[0]["commands"], commands)
        for module in ("policy", "os10_plan", "os10_safety"):
            with self.subTest(module=module):
                with self.assertRaises(ImportError):
                    __import__(f"changes.{module}")

    def test_verification_commands_are_read_only(self):
        self.assertEqual(B.normalize_verification_commands([" show vlan ", "SHOW running-configuration interface ethernet1/1/18"]),
                         ["show vlan", "SHOW running-configuration interface ethernet1/1/18"])
        self.assertEqual(B.normalize_verification_commands(None), [])
        for bad in (["reload"], ["show"], ["configure terminal"], ["show vlan\nreload"], [3], "show vlan", ["show x"] * 21):
            with self.subTest(bad=bad):
                with self.assertRaises(B.BlockError):
                    B.normalize_verification_commands(bad)

    def test_legacy_adapter(self):
        self.assertEqual(B.blocks_from_legacy(["description X"], ["interface Tw1/0/3"]),
                         [{"order": 0, "parent": "interface Tw1/0/3", "commands": ["description X"]}])
        self.assertEqual(B.blocks_from_legacy(["vlan 200"], None), [{"order": 0, "parent": None, "commands": ["vlan 200"]}])
        # Nested legacy parents keep their execution order (enter p1, then p2, then lines).
        self.assertEqual(B.blocks_from_legacy(["neighbor x"], ["router bgp 1", "address-family ipv4"]),
                         [{"order": 0, "parent": "router bgp 1", "commands": ["address-family ipv4", "neighbor x"]}])
        record = {"config_blocks": None, "config_lines": ["shutdown"], "config_parents": ["interface Tw1/0/3"]}
        self.assertEqual(B.stored_blocks(record)[0]["parent"], "interface Tw1/0/3")

    def test_cli_preview_and_flat_lines(self):
        blocks = B.normalize_blocks(MULTI)
        self.assertEqual(B.render_cli(blocks), "\n".join([
            "! Block 1", "interface ethernet 1/1/18", " description APP-SERVER", " switchport mode trunk",
            " switchport trunk allowed vlan 10,20,30", "", "! Block 2", "interface ethernet1/1/19", " description USER-PC",
            " switchport mode access", " switchport access vlan 40", "", "! Block 3", "router ospf 1", " router-id 10.0.0.1",
            "", "! Block 4 - Global", "ip routing"]))
        self.assertEqual(B.flatten(blocks)[:2], ["interface ethernet 1/1/18", "description APP-SERVER"])
        self.assertEqual(B.command_count(blocks), 8)

    def test_apply_steps_enter_and_leave_each_block(self):
        steps = B.apply_steps(B.normalize_blocks(MULTI[2:]), "configure terminal")
        self.assertEqual([(s["block"], s["kind"], s["command"]) for s in steps], [
            (0, "configure", "configure terminal"), (0, "parent", "router ospf 1"), (0, "command", "router-id 10.0.0.1"),
            (0, "end", "end"), (1, "configure", "configure terminal"), (1, "command", "ip routing"), (1, "end", "end")])
        self.assertEqual([s["i"] for s in steps], list(range(7)))
        self.assertEqual(platforms.platform_for("dell_os6").configure_command, "configure")
        self.assertEqual(platforms.platform_for("dell_os10").configure_command, "configure terminal")


# ---- semantic verification -------------------------------------------------------------------
class VerifyTests(unittest.TestCase):
    def statuses(self, platform, blocks, config, vlan_ids=None):
        report = verify.verify_blocks(platform, B.normalize_blocks(blocks), config, vlan_ids)
        return [[c["status"] for c in b["commands"]] for b in report]

    def test_os10_known_commands(self):
        self.assertEqual(self.statuses("dell_os10", [
            {"parent": "interface ethernet 1/1/18", "commands": ["switchport access vlan 20", "switchport access vlan 30",
                                                                  "switchport mode access", "no shutdown", "shutdown",
                                                                  "no description", "description X"]},
            {"parent": "interface ethernet1/1/19", "commands": ["switchport trunk allowed vlan 10", "switchport trunk allowed vlan 40",
                                                                "no switchport trunk allowed vlan 40", "switchport mode trunk"]},
        ], OS10_CONFIG), [["verified", "not_present", "verified", "verified", "not_present", "verified", "not_present"],
                          ["verified", "not_present", "verified", "verified"]])

    def test_arbitrary_commands_are_not_guessed(self):
        result = verify.verify_blocks("dell_os10", B.normalize_blocks([
            {"parent": "router bgp 65000", "commands": ["neighbor 192.0.2.2 remote-as 65001"]},
            {"parent": None, "commands": ["hostname Kenda-Core-1", "spanning-tree mode rstp", "no ip domain-lookup"]}]),
            OS10_CONFIG)
        self.assertEqual([[c["status"] for c in b["commands"]] for b in result],
                         [["not_available"], ["verified", "not_available", "verified"]])
        self.assertEqual(result[0]["commands"][0]["detail"], verify.NOT_AVAILABLE_TEXT)
        self.assertFalse(result[0]["found"])

    def test_os6_commands(self):
        self.assertEqual(self.statuses("dell_os6", [
            {"parent": "interface Tw1/0/3", "commands": ["description PRECHECK-TEST", 'description "PRECHECK-TEST"',
                                                        "switchport access vlan 10", "no shutdown", "shutdown"]},
            {"parent": "interface Tw1/0/12", "commands": ["shutdown", "no description"]},
            {"parent": "interface Tw1/0/20", "commands": ["description NEW", "no shutdown"]},  # default config: not listed
            {"parent": None, "commands": ["vlan 10", "vlan 50", "no vlan 20", "no vlan 60", "ip routing", "no ip routing"]},
        ], OS6_CONFIG, vlan_ids={1, 10, 20}), [
            ["verified", "verified", "verified", "verified", "not_present"], ["verified", "verified"],
            ["not_present", "verified"], ["verified", "not_present", "not_present", "verified", "verified", "not_present"]])

    def test_ambiguous_switchport_state_is_not_guessed(self):
        result = verify.verify_blocks("dell_os10", B.normalize_blocks([
            {"parent": "interface ethernet1/1/19", "commands": ["switchport access vlan 20", "switchport mode trunk"]}]),
            fixture("running_config_ambiguous.txt"))[0]["commands"]
        self.assertEqual([c["status"] for c in result], ["not_available", "not_available"])
        self.assertIn("more than one access VLAN line", result[0]["detail"])

    def test_os10_missing_physical_interface_is_a_precheck_failure(self):
        blocks = B.normalize_blocks([{"parent": "interface ethernet1/1/99", "commands": ["x"]},
                                     {"parent": "interface vlan 77", "commands": ["x"]}])
        found = [e["found"] for e in verify.verify_blocks("dell_os10", blocks, OS10_CONFIG)]
        self.assertEqual(found, [False, False])
        self.assertTrue(verify.precheck_block_issues("dell_os10", blocks[0], False))
        self.assertFalse(verify.precheck_block_issues("dell_os10", blocks[1], False))  # a new SVI is fine
        self.assertFalse(verify.precheck_block_issues("dell_os6", {"parent": "interface Tw1/0/40", "commands": []}, False))

    def test_semantic_status(self):
        r = lambda *s: [{"status": x} for x in s]
        self.assertEqual(verify.semantic_status(r("verified", "verified")), "verified")
        self.assertEqual(verify.semantic_status(r("verified", "not_available")), "partial")
        self.assertEqual(verify.semantic_status(r("not_available")), "not_available")
        self.assertEqual(verify.semantic_status(r("verified", "not_present")), "failed")
        self.assertEqual(verify.semantic_status([]), "not_available")


# ---- execution interpretation ----------------------------------------------------------------
class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.blocks = B.normalize_blocks(MULTI)
        self.steps = B.apply_steps(self.blocks, "configure terminal")

    def run_with(self, applied, failed=None, error="", rc=None):
        out = marker(applied, failed, error)
        return exe.interpret(self.blocks, self.steps, {"returncode": rc if rc is not None else (0 if failed is None else 2),
                                                        "stdout": out, "progress": parse_marker(out)})

    def test_success(self):
        result = self.run_with(list(range(len(self.steps))))
        self.assertEqual((result["outcome"], [b["status"] for b in result["blocks"]]), ("success", ["applied"] * 4))

    def test_partial_apply(self):
        first = [s["i"] for s in self.steps if s["block"] == 0]
        failed_step = next(s for s in self.steps if s["block"] == 1 and s["command"] == "switchport mode access")
        applied = first + [s["i"] for s in self.steps if s["block"] == 1 and s["i"] < failed_step["i"]]
        result = self.run_with(applied, failed_step["i"], "b'switchport mode access\\r\\n% Error: Invalid VLAN.\\r\\nx(conf)# '")
        self.assertEqual([b["status"] for b in result["blocks"]], ["applied", "failed", "not_attempted", "not_attempted"])
        self.assertEqual((result["outcome"], result["blocks"][1]["failed_command"]), ("partial", 2))
        self.assertEqual(result["blocks"][1]["error"], "% Error: Invalid VLAN.")
        self.assertEqual(result["error"], "Block 2 command 2 rejected: % Error: Invalid VLAN.")

    def test_failure_before_anything_applied_and_unknown(self):
        self.assertEqual(self.run_with([], 0, "unable to connect to port 22")["outcome"], "failed")
        result = exe.interpret(self.blocks, self.steps, {"returncode": -1, "stdout": "", "stderr": "timed out", "progress": None})
        self.assertEqual((result["outcome"], {b["status"] for b in result["blocks"]}), ("unknown", {"unknown"}))
        self.assertEqual(self.run_with(list(range(len(self.steps))), rc=4)["outcome"], "unknown")

    def test_marker_parsing(self):
        self.assertEqual(parse_marker(marker([0, 1], 2, "x")), {"applied": [0, 1], "failed": 2, "error": "x"})
        self.assertEqual(parse_marker(marker([0], "3")), {"applied": [0], "failed": 3, "error": ""})
        self.assertIsNone(parse_marker("no marker here"))
        self.assertIsNone(parse_marker("NMP_APPLY_RESULT:!!!notbase64"))

    def test_redaction_and_device_errors(self):
        self.assertEqual(exe.redact("username a password s3cret role x"), "username a password <redacted> role x")
        self.assertEqual(exe.redact("snmp-server community public ro"), "snmp-server community <redacted> ro")
        self.assertEqual(exe.redact("tacacs-server host 1.2.3.4 key 7 abc"), "tacacs-server host 1.2.3.4 key 7 <redacted>")
        self.assertEqual(exe.device_error("b'username x password hunter2\\r\\n% Invalid input detected at '"),
                         "% Invalid input detected at")
        self.assertNotIn("hunter2", exe.device_error("username x password hunter2 failed"))
        self.assertTrue(exe.truncate_output("x" * 30000).endswith("more characters)"))


# ---- workflow state machine (mocked) ---------------------------------------------------------
class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.redis = fake_redis.install(self)  # in-memory device coordination
        p = lambda name, **kw: mock.patch.object(workflow, name, **kw).start()
        self.pre_apply_backup = p("_pre_apply_backup", return_value={"status": "success", "job_id": "pre-1"})
        self.post_health = p("_post_change_health", return_value={"ok": True, "status": "healthy"})
        p("release_apply_claim", return_value=True)
        self.device_by_host = p("get_device_by_hostname", return_value=dict(OS10_DEVICE))
        self.device_by_id = p("get_device_by_id", return_value=dict(OS10_DEVICE))
        self.create_approval = p("create_change_approval", return_value={"approval_id": APPROVAL, "status": "pending"})
        self.audit = p("create_audit_event")
        self.get_approval = p("get_change_approval")
        self.verify_backup = p("verify_backup_for_device", return_value={"valid": True})
        self.applying = p("mark_approval_applying")
        self.applied = p("mark_approval_applied")
        self.failed = p("mark_approval_failed")
        self.begun = []
        self.begin = p("begin_execution", side_effect=lambda a, r: self.begun.append(json.loads(json.dumps(r))) or True)
        self.results = []
        p("set_execution_result", side_effect=lambda a, r: self.results.append(json.loads(json.dumps(r))))
        p("create_job", return_value="job-1")
        self.job_ok, self.job_failed = p("mark_job_success"), p("mark_job_failed")
        self.backup = mock.Mock(return_value={"status": "success", "job_id": BACKUP_JOB})
        self.state = {"running_config": OS10_CONFIG, "vlan_ids": None}
        for cls in (platforms.Os10Platform, platforms.Os6Platform):
            mock.patch.object(cls, "read_device_state", side_effect=lambda host, blocks: dict(self.state)).start()
        self.apply_run = mock.patch.object(platforms.Os10Platform, "apply_blocks", side_effect=self._apply).start()
        self.os6_apply = mock.patch.object(platforms.Os6Platform, "apply_blocks", side_effect=self._apply_os6).start()
        self.show = mock.patch.object(platforms.Os10Platform, "run_show_commands",
                                      side_effect=lambda h, c: [{"command": x, "failed": False, "output": "community public ok"} for x in c]).start()
        self.fail_at = None
        self.after_config = None

    def _run(self, blocks, configure):
        steps = B.apply_steps(blocks, configure)
        if self.fail_at is None:
            applied, failed = [s["i"] for s in steps], None
        else:
            failed = next(s["i"] for s in steps if s["block"] == self.fail_at[0] and s["command"] == self.fail_at[1])
            applied = list(range(failed))
        if self.after_config is not None:
            self.state["running_config"] = self.after_config
        out = marker(applied, failed, "% Error: rejected")
        return steps, {"returncode": 0 if failed is None else 2, "stdout": out, "stderr": "", "progress": parse_marker(out)}

    def _apply(self, host, blocks):
        return self._run(blocks, "configure terminal")

    def _apply_os6(self, host, blocks):
        return self._run(blocks, "configure")

    def events(self):
        return [c.kwargs["event_type"] for c in self.audit.call_args_list]

    def approval(self, status="applying", device=OS10_DEVICE, blocks=None, verification=None, lines=None, parents=None,
                 execution=None):
        self.device_by_id.return_value = dict(device)
        record = {"approval_id": APPROVAL, "device_id": device["id"], "backup_job_id": BACKUP_JOB, "status": status,
                  "approved_by": "approver", "config_blocks": B.normalize_blocks(blocks) if blocks else None,
                  "config_lines": lines or (B.flatten(B.normalize_blocks(blocks)) if blocks else None),
                  "config_parents": parents, "verification_commands": verification or [], "execution_result": execution}
        self.get_approval.return_value = record
        return record


class PrecheckTests(WorkflowTestCase):
    def test_multi_block_precheck_creates_one_approval(self):
        result = workflow.run_precheck("Kenda-Core-1", config_blocks=MULTI, verification_commands=["show vlan"], backup=self.backup)
        self.assertEqual((result["status"], result["block_count"], result["command_count"]), ("pending_approval", 4, 8))
        dry = result["dry_run"]
        self.assertEqual(dry["checks"], {"structural_validation": "PASS", "device_connectivity": "PASS",
                                         "current_config_capture": "PASS", "semantic_verification": "PARTIAL"})
        self.assertEqual([b["status"] for b in dry["blocks"]], ["PASS"] * 4)
        self.assertTrue(dry["cli_preview"].startswith("! Block 1\ninterface ethernet 1/1/18"))
        kwargs = self.create_approval.call_args.kwargs
        self.assertEqual(kwargs["config_blocks"], B.normalize_blocks(MULTI))
        self.assertEqual((kwargs["config_parents"], kwargs["verification_commands"]), (None, ["show vlan"]))
        self.assertEqual(kwargs["config_lines"], B.flatten(B.normalize_blocks(MULTI)))
        self.assertEqual(kwargs["precheck_summary"]["block_count"], 4)
        self.backup.assert_called_once_with("Kenda-Core-1", config_snapshot=OS10_CONFIG)
        self.assertEqual(self.events(), ["precheck_completed", "change_created"])
        self.apply_run.assert_not_called()  # precheck never writes

    def test_one_failing_block_fails_the_whole_precheck(self):
        blocks = MULTI + [{"parent": "interface ethernet1/1/99", "commands": ["description X"]}]
        result = workflow.run_precheck("Kenda-Core-1", config_blocks=blocks, backup=self.backup)
        self.assertEqual((result["status"], result["ready_for_approval"]), ("precheck_failed", False))
        self.assertEqual([b["status"] for b in result["dry_run"]["blocks"]], ["PASS"] * 4 + ["FAILED"])
        self.assertEqual(result["rejection_reasons"], ["Block 5: interface ethernet1/1/99 was not found on the device"])
        self.backup.assert_not_called()
        self.create_approval.assert_not_called()
        self.assertEqual(self.events(), ["precheck_failed"])

    def test_structural_problems_fail_before_device_access(self):
        with self.assertRaises(B.BlockError):
            workflow.run_precheck("Kenda-Core-1", config_blocks=[{"parent": None, "commands": []}], backup=self.backup)
        with self.assertRaises(B.BlockError):
            workflow.run_precheck("Kenda-Core-1", config_blocks=MULTI, verification_commands=["reload"], backup=self.backup)
        platforms.Os10Platform.read_device_state.assert_not_called()
        self.assertEqual(self.events(), ["precheck_failed", "precheck_failed"])

    def test_no_change_required_only_when_everything_verifies(self):
        result = workflow.run_precheck("Kenda-Core-1", config_blocks=[{"parent": "interface ethernet1/1/18",
                                                                       "commands": ["switchport access vlan 20", "no shutdown"]}],
                                       backup=self.backup)
        self.assertEqual(result["status"], "no_change_required")
        self.backup.assert_not_called()
        result = workflow.run_precheck("Kenda-Core-1", config_blocks=[{"parent": None, "commands": ["spanning-tree mode rstp"]}],
                                       backup=self.backup)
        self.assertEqual(result["status"], "pending_approval")  # cannot be verified -> sent as written
        self.assertEqual(result["dry_run"]["checks"]["semantic_verification"], "NOT AVAILABLE")

    def test_read_failures_are_sanitized(self):
        platforms.Os10Platform.read_device_state.side_effect = RuntimeError("NetmikoAuthenticationException password=hunter2")
        with self.assertRaises(workflow.ChangeError) as ctx:
            workflow.run_precheck("Kenda-Core-1", config_blocks=MULTI, backup=self.backup)
        self.assertEqual(str(ctx.exception), "Could not read the running configuration: SSH authentication failed")

    def test_backup_failure_prevents_approval(self):
        self.backup.return_value = {"status": "failed"}
        with self.assertRaisesRegex(RuntimeError, "Backup failed"):
            workflow.run_precheck("Kenda-Core-1", config_blocks=MULTI, backup=self.backup)
        self.create_approval.assert_not_called()

    def test_os6_and_legacy_requests(self):
        self.device_by_host.return_value = dict(OS6_DEVICE)
        self.state = {"running_config": OS6_CONFIG, "vlan_ids": {1, 10, 20}}
        result = workflow.run_precheck("Kenda-HARO-IDF-A", ["shutdown"], ["interface Tw1/0/3"], backup=self.backup,
                                       expected_platform="dell_os6")
        self.assertEqual((result["status"], result["platform"]), ("pending_approval", "dell_os6"))
        self.assertEqual(self.create_approval.call_args.kwargs["config_blocks"],
                         [{"order": 0, "parent": "interface Tw1/0/3", "commands": ["shutdown"]}])
        with self.assertRaisesRegex(ValueError, "not dell_os6"):
            self.device_by_host.return_value = dict(OS10_DEVICE)
            workflow.run_precheck("Kenda-Core-1", config_blocks=MULTI, backup=self.backup, expected_platform="dell_os6")

    def test_unsupported_platform(self):
        self.device_by_host.return_value = {**OS10_DEVICE, "platform": "cisco_ios"}
        with self.assertRaises(platforms.UnsupportedPlatform):
            workflow.run_precheck("Kenda-Core-1", config_blocks=MULTI, backup=self.backup)


class ApplyTests(WorkflowTestCase):
    APPLIED_CONFIG = OS10_CONFIG.replace(
        "interface ethernet1/1/18\n no shutdown\n switchport access vlan 20",
        "interface ethernet1/1/18\n description APP-SERVER\n no shutdown\n switchport mode trunk\n switchport access vlan 20\n"
        " switchport trunk allowed vlan 10,20,30").replace(
        "interface ethernet1/1/19\n no shutdown\n switchport mode trunk\n switchport access vlan 1\n switchport trunk allowed vlan 10,20",
        "interface ethernet1/1/19\n description USER-PC\n no shutdown\n switchport access vlan 40")

    def test_success_applies_blocks_in_order_and_verifies(self):
        self.approval(blocks=MULTI, verification=["show snmp community"])
        self.after_config = self.APPLIED_CONFIG
        result = workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.assertEqual(result["status"], "applied")
        sent = self.apply_run.call_args.args[1]
        self.assertEqual(sent, B.normalize_blocks(MULTI))  # exactly the approved blocks, in order
        final = self.results[-1]
        self.assertEqual((final["outcome"], final["execution"], final["semantic"]), ("applied", "success", "partial"))
        self.assertEqual([b["status"] for b in final["blocks"]], ["applied"] * 4)
        self.assertEqual(final["verification"], [{"command": "show snmp community", "ok": True, "output": "community <redacted> ok"}])
        self.assertEqual(final["job_id"], "job-1")
        self.applied.assert_called_once_with(APPROVAL)
        self.job_ok.assert_called_once_with("job-1")
        self.assertEqual(self.events(), ["apply_started"] + ["apply_block_completed"] * 4 + ["postcheck_completed", "apply_completed"])
        self.applying.assert_not_called()  # the API already claimed it

    def test_progress_is_persisted_before_and_after_execution(self):
        self.approval(blocks=MULTI)
        self.after_config = self.APPLIED_CONFIG
        workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.assertEqual([b["status"] for b in self.begun[0]["blocks"]], ["applying"] * 4)
        self.assertEqual(self.results[0]["job_id"], "job-1")
        self.assertEqual(self.results[1]["pre_apply_backup"], {"status": "success", "job_id": "pre-1"})  # backup first
        self.assertEqual(self.results[1]["execution"], "applying")
        self.assertEqual(self.results[2]["execution"], "success")
        self.assertEqual(self.results[-1]["post_change_health"], {"ok": True, "status": "healthy"})

    def test_partial_apply_is_reported_block_by_block(self):
        self.approval(blocks=MULTI)
        self.fail_at = (1, "switchport mode access")
        self.after_config = self.APPLIED_CONFIG
        with self.assertRaisesRegex(workflow.ChangeError, r"^PARTIAL APPLY on Kenda-Core-1: block 1 applied, block 2 failed, "
                                                          r"block 3 not attempted, block 4 not attempted\. Block 2 command 2 "
                                                          r"rejected: % Error: rejected\. Applied blocks were not rolled back\."):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        final = self.results[-1]
        self.assertEqual((final["outcome"], final["execution"]), ("partial_apply", "partial"))
        self.assertEqual([b["index"] for b in final["post_check"]["blocks"]], [0])  # only applied blocks are verified
        self.failed.assert_called_once_with(APPROVAL)
        self.job_failed.assert_called_once()
        self.applied.assert_not_called()
        self.assertEqual(self.events(), ["apply_started", "apply_block_completed", "apply_block_failed",
                                         "apply_block_not_attempted", "apply_block_not_attempted", "postcheck_completed",
                                         "apply_failed"])

    def test_postcheck_mismatch_fails(self):
        self.approval(blocks=MULTI)  # device still shows the old configuration
        with self.assertRaisesRegex(workflow.ChangeError, r"Post-check failed .*\(missing: description APP-SERVER"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.assertEqual((self.results[-1]["execution"], self.results[-1]["semantic"]), ("success", "failed"))

    def test_postcheck_read_failure_says_verify_manually(self):
        self.approval(blocks=MULTI)
        platforms.Os10Platform.read_device_state.side_effect = RuntimeError("Unable to connect to port 22")
        with self.assertRaisesRegex(workflow.ChangeError, "may have been applied - verify manually: Device unreachable"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)

    def test_unknown_outcome_after_timeout(self):
        self.approval(blocks=MULTI)
        self.apply_run.side_effect = lambda h, b: (B.apply_steps(b, "configure terminal"),
                                                   {"returncode": -1, "stdout": "", "stderr": "ansible-playbook timed out after 200 s",
                                                    "progress": None})
        with self.assertRaisesRegex(workflow.ChangeError, "Apply result unknown .*verify manually"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.assertEqual({b["status"] for b in self.results[-1]["blocks"]}, {"unknown"})

    def test_runner_error_before_anything_is_sent(self):
        self.approval(blocks=MULTI)
        self.apply_run.side_effect = type("Forbidden", (Exception,), {"__module__": "hvac.exceptions"})("denied")
        with self.assertRaisesRegex(workflow.ChangeError, "no block was applied.*Credential lookup failed"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.assertEqual({b["status"] for b in self.results[-1]["blocks"]}, {"not_attempted"})

    def test_duplicate_execution_is_refused(self):
        self.approval(blocks=MULTI)
        self.begin.side_effect = None
        self.begin.return_value = False
        with self.assertRaisesRegex(ValueError, "already has an apply attempt"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.apply_run.assert_not_called()
        self.failed.assert_not_called()

    def test_wrong_states_never_reach_the_device(self):
        for status in ("pending", "approved", "cancelled", "applied", "failed"):
            with self.subTest(status=status):
                self.approval(status=status, blocks=MULTI)
                with self.assertRaisesRegex(ValueError, f"is {status}, expected applying"):
                    workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.apply_run.assert_not_called()
        self.failed.assert_not_called()

    def test_invalid_backup_or_corrupt_stored_blocks_fail_before_device(self):
        self.approval(blocks=MULTI)
        self.verify_backup.return_value = {"valid": False, "reason": "Backup job status is failed"}
        with self.assertRaisesRegex(ValueError, "Backup verification failed"):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.verify_backup.return_value = {"valid": True}
        record = self.approval(blocks=MULTI)
        record["config_blocks"] = [{"parent": None, "commands": ["ok\nreload"]}]
        with self.assertRaises(B.BlockError):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        self.apply_run.assert_not_called()
        self.assertEqual(self.failed.call_count, 2)
        self.assertEqual(self.events(), ["apply_failed", "apply_failed"])

    def test_legacy_single_parent_approval_still_applies(self):
        self.device_by_id.return_value = dict(OS6_DEVICE)
        self.state = {"running_config": OS6_CONFIG.replace('description "PRECHECK-TEST"', 'description "LEGACY"'), "vlan_ids": None}
        self.approval(device=OS6_DEVICE, lines=["description LEGACY"], parents=["interface Tw1/0/3"])
        result = workflow.run_apply(APPROVAL, claimed_by_api=True, expected_platform="dell_os6")
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.os6_apply.call_args.args[1],
                         [{"order": 0, "parent": "interface Tw1/0/3", "commands": ["description LEGACY"]}])

    def test_direct_apply_claims_approved_and_platform_guard(self):
        self.approval(status="approved", blocks=MULTI)
        self.after_config = self.APPLIED_CONFIG
        workflow.run_apply(APPROVAL, claimed_by_api=False)
        self.applying.assert_called_once_with(APPROVAL)
        self.approval(blocks=MULTI)
        with self.assertRaisesRegex(ValueError, "not a dell_os6 device"):
            workflow.run_apply(APPROVAL, claimed_by_api=True, expected_platform="dell_os6")

    def test_audit_never_contains_secret_values(self):
        blocks = [{"parent": None, "commands": ["username netops password Sup3rSecret role sysadmin"]}]
        self.approval(blocks=blocks)
        self.fail_at = (0, blocks[0]["commands"][0])
        with self.assertRaises(workflow.ChangeError):
            workflow.run_apply(APPROVAL, claimed_by_api=True)
        messages = " ".join(c.kwargs["message"] for c in self.audit.call_args_list)
        self.assertNotIn("Sup3rSecret", messages + json.dumps(self.results))


class AnsibleSummaryTests(unittest.TestCase):
    def test_summaries(self):
        summary = workflow.ansible_failure_summary({"returncode": 2, "stdout": fixture("ansible_apply_device_error.txt")})
        self.assertEqual(summary, "Ansible apply failed (rc=2): % Error: Command rejected by simulated device.")
        self.assertEqual(workflow.ansible_failure_summary({"returncode": -1, "stdout": "", "stderr": "ansible-playbook timed out after 180 s"}),
                         "Ansible apply failed (rc=-1): ansible-playbook timed out after 180 s")


class WorkerTaskRegistrationTests(unittest.TestCase):
    def test_generic_and_legacy_tasks(self):
        import worker

        for name in ("network_worker.change_precheck", "network_worker.apply_approved_change",
                     "network_worker.os6_change_precheck", "network_worker.os6_apply_approved_change"):
            self.assertIn(name, worker.app.tasks)
        with mock.patch.object(worker, "run_change_precheck") as run:
            worker.change_precheck("Kenda-Core-1", config_blocks=MULTI, verification_commands=["show vlan"])
            self.assertEqual(run.call_args.kwargs["config_blocks"], MULTI)
            self.assertIs(run.call_args.kwargs["backup"], worker.backup_running_config_task)
            worker.change_precheck("Kenda-Core-1", ["description X"], ["interface ethernet1/1/5"])  # older API message
            self.assertEqual(run.call_args.args[1:], (["description X"], ["interface ethernet1/1/5"]))
        with mock.patch.object(worker, "run_change_apply") as run:
            worker.os6_apply_approved_change("x", claimed_by_api=True)
            run.assert_called_once_with("x", claimed_by_api=True, expected_platform="dell_os6")


if __name__ == "__main__":
    unittest.main()
