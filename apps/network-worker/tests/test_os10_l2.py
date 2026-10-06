"""
Dell OS10 safe-L2 (V1.1): VLAN parsing, operator and device grammars, running-config state
parsing, interface safety classification, planner (precheck / idempotency / diff),
post-check per command, and mocked end-to-end workflow flows A-J. No switch is contacted:
fixtures are synthetic (tests/fixtures/os10/README.md) and the workflow flows drive the
simulated OS10 device model in-process (no SSH). Real Netmiko/Ansible flows are in
test_os10_simulated.py; real PostgreSQL + API flows in the API's test_os10_change_flow_pg.py.
    python -m unittest tests.test_os10_l2 -v
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from changes import os10, os10_plan, os10_safety, platforms, workflow  # noqa: E402
from changes.policy import PolicyViolation, change_kinds, check_os10, check_os10_commands  # noqa: E402
from changes.vlans import VlanListError, format_vlan_list, parse_vlan_id, parse_vlan_list  # noqa: E402
from fakes.os10_device import FakeOS10  # noqa: E402
from test_change_workflow import APPROVAL, BACKUP_JOB, OS10_DEVICE, WorkflowTestCase  # noqa: E402
from topology.lldp import parse_lldp_output  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "os10")


def fixture(name):
    with open(os.path.join(FIX, name)) as handle:
        return handle.read()


L2 = fixture("running_config_l2.txt")
INBAND = fixture("running_config_inband_mgmt.txt")
AMBIGUOUS = fixture("running_config_ambiguous.txt")
LLDP = fixture("show_lldp_neighbors_l2.txt")
MGMT_IP = "192.0.2.10"


def neighbors_on(interface, network_device=None):
    parsed, warnings = parse_lldp_output("dell_os10", LLDP)
    assert not warnings
    on = [n for n in parsed if n["local_interface"] == interface]
    for n in on:
        if network_device:
            n.update(remote_device_id=13, remote_hostname=network_device)
    return on


def context(interface, network_device=None, **overrides):
    ctx = {"management_ip": MGMT_IP, "protected": set(), "protected_error": None,
           "lldp_neighbors": neighbors_on(interface, network_device), "lldp_error": None,
           "stored_links": [], "stored_error": None}
    ctx.update(overrides)
    return ctx


def plan(lines, interface="ethernet1/1/18", config=L2, **ctx_overrides):
    canonical, parents = check_os10(lines, [f"interface {interface}"])
    return os10_plan.plan(canonical, parents, config, context(interface, **ctx_overrides))


def replace_block(config, interface, body):
    """Config with one interface block's body replaced (for post-check cases)."""
    lines, out, skipping = config.splitlines(), [], False
    for line in lines:
        if line == f"interface {interface}":
            out.append(line)
            out += [f" {b}" for b in body]
            skipping = True
            continue
        if skipping and line.startswith(" "):
            continue
        skipping = False
        out.append(line)
    return "\n".join(out)


def violations(lines, parents=("interface ethernet1/1/18",)):
    with self_raises() as ctx:
        check_os10(lines, list(parents))
    return ctx.exception.violations


class self_raises:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not PolicyViolation:
            raise AssertionError(f"expected PolicyViolation, got {exc_type}")
        self.exception = exc
        return True


# ---- Phase 3: VLAN validation ---------------------------------------------------------------
class VlanTests(unittest.TestCase):
    def test_valid_lists_are_normalized(self):
        cases = {"10": (10,), "10,20,30": (10, 20, 30), "10-12": (10, 11, 12), "10,20-22,40": (10, 20, 21, 22, 40),
                 "1": (1,), "4094": (4094,), "30,10,10": (10, 30), "4093-4094": (4093, 4094)}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_vlan_list(text), expected)
        self.assertEqual(format_vlan_list((40, 10, 11, 12, 20)), "10-12,20,40")
        self.assertEqual(format_vlan_list((10, 11)), "10-11")
        self.assertEqual(format_vlan_list(()), "")
        self.assertEqual(parse_vlan_id("20"), 20)

    def test_invalid_lists_are_rejected(self):
        for text in ("0", "4095", "-1", "abc", "10-5", "10-10", "10,,20", "", " ", "10, 20", "010", "1-4095", "10-",
                     "+5", "1e3", "10;20", "10 20", ",10", "10,", "5000"):
            with self.subTest(text=text):
                with self.assertRaises(VlanListError):
                    parse_vlan_list(text)
        for text in ("0", "4095", "-1", "abc", "10,20", ""):
            with self.assertRaises(VlanListError):
                parse_vlan_id(text)

    def test_pathological_inputs_are_rejected(self):
        with self.assertRaisesRegex(VlanListError, "more than 256 VLANs"):
            parse_vlan_list("1-300")
        with self.assertRaisesRegex(VlanListError, "more than 32 items"):
            parse_vlan_list(",".join(str(v) for v in range(1, 67, 2)))
        with self.assertRaisesRegex(VlanListError, "longer than 200"):
            parse_vlan_list(",".join(["4094"] * 50))
        self.assertEqual(len(parse_vlan_list("1-4094", device_limits=True)), 4094)  # device output is not capped


# ---- Phases 2, 6, 12, 16: operator grammar ----------------------------------------------------
class PolicyAllowedTests(unittest.TestCase):
    def test_supported_commands(self):
        for line in ("description test", "no description", "switchport mode access", "switchport access vlan 20",
                     "switchport mode trunk", "switchport trunk allowed vlan 10,20,30",
                     "switchport trunk allowed vlan add 40", "switchport trunk allowed vlan remove 10-12",
                     "shutdown", "no shutdown"):
            with self.subTest(line=line):
                self.assertEqual(check_os10([line], ["interface ethernet1/1/18"]), ([line], ["interface ethernet1/1/18"]))

    def test_normalization(self):
        self.assertEqual(check_os10(["Switchport  Mode   TRUNK", "switchport trunk allowed vlan 30,10,20-21"],
                                    ["INTERFACE Ethernet 1/1/18"]),
                         (["switchport mode trunk", "switchport trunk allowed vlan 10,20-21,30"], ["interface ethernet1/1/18"]))
        self.assertEqual(check_os10(["SHUTDOWN"], ["interface ethernet1/1/26:2"])[0], ["shutdown"])

    def test_canonical_order_and_idempotent_normalization(self):
        lines, parents = check_os10(["no shutdown", "switchport access vlan 20", "description X", "switchport mode access"],
                                    ["interface ethernet 1/1/18"])
        self.assertEqual(lines, ["description X", "switchport mode access", "switchport access vlan 20", "no shutdown"])
        self.assertEqual(check_os10(lines, parents), (lines, parents))

    def test_change_kinds(self):
        self.assertEqual(change_kinds(["description X"]), {"description"})
        self.assertEqual(change_kinds(["shutdown", "switchport mode trunk", "no switchport trunk allowed vlan 10"]),
                         {"shutdown", "switchport"})
        self.assertEqual(change_kinds(["no shutdown", "no description"]), {"no_shutdown", "description"})


class PolicyRejectedTests(unittest.TestCase):
    def test_blocked_commands_with_reasons(self):
        cases = {
            "no interface ethernet1/1/18": "interface", "interface ethernet1/1/19": "interface",
            "default interface ethernet1/1/18": "default", "reload": "system", "reboot": "system",
            "write erase": "system", "delete startup-config": "system", "restore factory-defaults": "system",
            "factory default": "system", "management route 0.0.0.0/0 192.0.2.1": "management",
            "interface mgmt1/1/1": "management", "router bgp 65000": "routing", "router ospf 1": "routing",
            "ip route 0.0.0.0/0 192.0.2.1": "routing", "ipv6 route ::/0 2001:db8::1": "routing",
            "aaa authentication login default local": "aaa", "tacacs-server host 192.0.2.5": "aaa",
            "radius-server host 192.0.2.6": "aaa", "username netops password x role sysadmin": "credentials",
            "password x": "credentials", "enable password x": "credentials", "ip ssh server enable": "ssh",
            "snmp-server community x ro": "snmp", "spanning-tree mode rstp": "spanning_tree",
            "spanning-tree port type edge": "spanning_tree", "vlt-domain 1": "vlt", "interface port-channel20": "port_channel",
            "channel-group 10 mode active": "port_channel", "no channel-group": "port_channel", "no vlan 20": "vlan",
            "vlan 20": "vlan", "no switchport": "switchport", "switchport trunk native vlan 10": "switchport",
            "no switchport trunk allowed vlan 10": "switchport", "switchport mode hybrid": "switchport",
            "mtu 9216": "mtu", "ip access-group BLOCK in": "acl", "service-policy input QOS": "qos",
            "ip address 192.0.2.9/24": "ip_address", "ip vrf forwarding management": "management",
            "speed 1000": "not_allowed",
        }
        for command, category in cases.items():
            with self.subTest(command=command):
                found = violations([command])
                self.assertEqual(found[0]["category"], category)
                self.assertTrue(found[0]["reason"])

    def test_bad_vlans(self):
        for command in ("switchport access vlan 4095", "switchport access vlan 0", "switchport access vlan -1",
                        "switchport access vlan abc", "switchport access vlan 10 20", "switchport access vlan",
                        "switchport trunk allowed vlan 10-5", "switchport trunk allowed vlan 10,,20",
                        "switchport trunk allowed vlan add", "switchport trunk allowed vlan 1-300",
                        "switchport trunk allowed vlan all", "switchport trunk allowed vlan except 10"):
            with self.subTest(command=command):
                self.assertEqual(violations([command])[0]["category"], "vlan_syntax")

    def test_parents(self):
        for parents in ([], ["interface ethernet1/1/18", "interface ethernet1/1/19"], ["int eth1/1/18"],
                        ["interface Eth1/1/18"], ["interface ethernet1/1"], ["interface range ethernet1/1/1-1/1/4"]):
            with self.subTest(parents=parents):
                self.assertIn("context", [v["category"] for v in violations(["description X"], parents)])
        self.assertEqual(violations(["description X"], ["interface mgmt1/1/1"])[0]["category"], "management")
        self.assertEqual(violations(["description X"], ["interface vlan10"])[0]["category"], "vlan")
        self.assertEqual(violations(["description X"], ["interface port-channel10"])[0]["category"], "port_channel")

    def test_contradictions_and_duplicates(self):
        for lines in (["switchport mode access", "switchport mode trunk"],
                      ["switchport access vlan 10", "switchport trunk allowed vlan 10,20"],
                      ["switchport mode trunk", "switchport access vlan 20"],
                      ["switchport mode access", "switchport trunk allowed vlan add 10"],
                      ["shutdown", "no shutdown"], ["description A", "no description"],
                      ["switchport access vlan 10", "switchport access vlan 20"],
                      ["switchport trunk allowed vlan add 10", "switchport trunk allowed vlan remove 20"]):
            with self.subTest(lines=lines):
                self.assertTrue(all(v["category"] == "context" for v in violations(lines)))

    def test_every_rejected_line_is_reported(self):
        found = violations(["reload", "description ok", "switchport access vlan 4095", "aaa new-model"])
        self.assertEqual([v["category"] for v in found], ["system", "vlan_syntax", "aaa"])


# ---- device grammar (what an approval stores) --------------------------------------------------
class DeviceGrammarTests(unittest.TestCase):
    P = ["interface ethernet1/1/18"]

    def test_planner_outputs_and_v1_approvals_are_accepted(self):
        for lines in (["description AUTOMATION-TEST"], ["no description"], ["shutdown", "description PARKED"],
                      ["switchport access vlan 30"], ["switchport mode trunk", "switchport trunk allowed vlan 30,40"],
                      ["switchport trunk allowed vlan 20,30", "no switchport trunk allowed vlan 10"],
                      ["no switchport trunk allowed vlan 10,20", "switchport mode access", "switchport access vlan 30"],
                      ["switchport mode access", "no shutdown"]):
            with self.subTest(lines=lines):
                self.assertEqual(check_os10_commands(lines, self.P), (lines, self.P))

    def test_non_canonical_or_tampered_commands_are_rejected(self):
        for lines in (["switchport trunk allowed vlan add 10"], ["switchport trunk allowed vlan 20,10"],
                      ['description "X"'], ["Shutdown"], ["no shutdown", "shutdown"], ["shutdown", "shutdown"],
                      ["switchport access vlan 20", "switchport mode access"],
                      ["no switchport trunk allowed vlan 10", "switchport trunk allowed vlan 20,30"],
                      ["switchport mode access", "switchport trunk allowed vlan 10"],
                      ["switchport mode trunk", "switchport access vlan 10"], ["no switchport"], ["reload"],
                      ["switchport access vlan 4095"], ["switchport trunk allowed vlan " + ",".join(map(str, range(1, 200, 2)))],
                      []):
            with self.subTest(lines=lines):
                with self.assertRaises(PolicyViolation):
                    check_os10_commands(lines, self.P)
        with self.assertRaises(PolicyViolation):
            check_os10_commands(["shutdown"], ["interface ethernet 1/1/18"])  # parent not canonical


# ---- running-configuration parsing --------------------------------------------------------------
class StateParsingTests(unittest.TestCase):
    def state(self, interface, config=L2):
        return os10.interface_state(config, f"interface {interface}")

    def test_fixture_interfaces(self):
        access = self.state("ethernet1/1/18")
        self.assertEqual((access["mode"], access["access_vlan"], access["admin"], access["allowed_vlans"]), ("access", 20, "up", ()))
        trunk = self.state("ethernet1/1/19")
        self.assertEqual((trunk["mode"], trunk["access_vlan"], trunk["allowed_vlans"]), ("trunk", 1, (10, 20)))
        shut = self.state("ethernet1/1/20")
        self.assertEqual((shut["admin"], shut["description"]), ("down", "AUTOMATION-TEST"))
        member = self.state("ethernet1/1/30:2")
        self.assertEqual((member["port_channel"], member["mode"]), ("10", None))
        routed = self.state("ethernet1/1/54")
        self.assertEqual((routed["mode"], routed["routed"], routed["ip_addresses"]), ("routed", True, ["198.51.100.1/31"]))
        self.assertEqual(os10.existing_vlans(L2), {1, 10, 20, 30, 40})

    def test_management_location(self):
        self.assertEqual(os10.management_location(L2, MGMT_IP)["kind"], "out_of_band")
        inband = os10.management_location(INBAND, MGMT_IP)
        self.assertEqual((inband["kind"], inband["vlan"]), ("vlan", 10))
        self.assertEqual(os10.management_location(fixture("running_config_description_absent.txt"), MGMT_IP)["kind"],
                         "out_of_band")  # DHCP on mgmt1/1/1 only
        self.assertEqual(os10.management_location(L2, "192.0.2.99")["kind"], "unknown")
        self.assertEqual(os10.management_location(L2, None)["kind"], "unknown")
        routed_mgmt = L2.replace("ip address 198.51.100.1/31", "ip address 192.0.2.10/24").replace(
            " ip address 192.0.2.10/24\n ipv6", " ipv6")
        self.assertEqual(os10.management_location(routed_mgmt, MGMT_IP),
                         {"kind": "interface", "interface": "ethernet1/1/54",
                          "detail": "management IP is on routed port ethernet1/1/54"})

    def test_vlt(self):
        self.assertEqual(os10.vlt_interfaces(L2), ({"ethernet1/1/49", "ethernet1/1/50"}, None))
        self.assertEqual(os10.vlt_interfaces(L2.replace("ethernet1/1/49-1/1/50", "ethernet1/1/49,ethernet1/1/52"))[0],
                         {"ethernet1/1/49", "ethernet1/1/52"})
        self.assertIsNotNone(os10.vlt_interfaces(L2.replace("ethernet1/1/49-1/1/50", "port-channel100"))[1])
        self.assertEqual(os10.vlt_interfaces(fixture("running_config_description_absent.txt")), (set(), None))

    def test_ambiguous_output_is_never_guessed(self):
        both = self.state("ethernet1/1/18", AMBIGUOUS)
        self.assertIsNone(both["admin"])
        self.assertIn("both `shutdown` and `no shutdown` present", both["admin_issues"])
        self.assertIsNone(self.state("ethernet1/1/19", AMBIGUOUS)["mode"])  # two access VLANs
        self.assertIn("unparseable trunk allowed-VLAN list", self.state("ethernet1/1/21", AMBIGUOUS)["switchport_issues"])
        bare = self.state("ethernet1/1/22", AMBIGUOUS)
        self.assertEqual((bare["admin"], bare["mode"]), (None, None))
        self.assertIsNone(self.state("ethernet1/1/23", AMBIGUOUS)["mode"])  # unrecognised switchport line

    def test_missing_interface_and_unrecognised_output(self):
        with self.assertRaisesRegex(os10.VerificationError, "not found"):
            self.state("ethernet1/1/99")
        with self.assertRaisesRegex(os10.VerificationError, "not recognisable"):
            os10.interface_state("% Error: Unrecognized command.", "interface ethernet1/1/18")


# ---- Phases 4, 5: interface safety classification ---------------------------------------------
class SafetyTests(unittest.TestCase):
    def reasons(self, lines, interface, **kw):
        return plan(lines, interface, **kw)["rejection_reasons"]

    def test_eligible_port_passes_every_check(self):
        result = plan(["switchport access vlan 30"])
        self.assertEqual((result["safety"]["status"], result["safety"]["classification"]), ("PASS", "eligible"))
        self.assertTrue(all(c["status"] == "pass" for c in result["safety"]["checks"]))
        self.assertEqual({c["name"] for c in result["safety"]["checks"]},
                         {"explicit protection", "management path", "routed port", "port-channel membership",
                          "VLT interconnect", "LLDP neighbor (live)", "LLDP neighbor (stored topology)", "switchport state"})

    def test_shutdown_on_uplink_is_rejected_with_exact_reason(self):
        self.assertEqual(self.reasons(["shutdown"], "ethernet1/1/25", network_device="Kenda-Core-2"),
                         ["shutdown rejected: ethernet1/1/25 is classified as an uplink (live LLDP: kenda-core-02 "
                          "ethernet1/1/25 = inventory device Kenda-Core-2)."])

    def test_neighbor_only_in_stored_topology(self):
        stored = [{"remote_system_name": "Kenda-HQ-IDF-A", "remote_interface": "Te1/0/2", "remote_device_id": 7,
                   "remote_hostname": "Kenda-HQ-IDF-A", "source": "stored (active)"}]
        reasons = self.reasons(["switchport access vlan 30"], "ethernet1/1/18", stored_links=stored)
        self.assertEqual(reasons, ["switchport change rejected: ethernet1/1/18 is classified as an uplink (stored topology: "
                                   "Kenda-HQ-IDF-A Te1/0/2 = inventory device Kenda-HQ-IDF-A)."])

    def test_unresolved_lldp_neighbor_blocks_l2_changes(self):
        reasons = self.reasons(["switchport access vlan 30"], "ethernet1/1/33")
        self.assertEqual(reasons, ["switchport change rejected: ethernet1/1/33 is classified as an LLDP-connected port "
                                   "(live LLDP: 50:7c:6f:4a:53:03 50:7c:6f:4a:53:03; cannot confirm the neighbor is not "
                                   "network infrastructure)."])

    def test_port_channel_member_routed_and_vlt(self):
        self.assertIn("is classified as a port-channel member (member of port-channel 10)",
                      self.reasons(["shutdown"], "ethernet1/1/30:2", lldp_neighbors=[])[0])
        self.assertIn("is classified as a routed port", self.reasons(["switchport mode trunk"], "ethernet1/1/54")[0])
        vlti = plan(["shutdown"], "ethernet1/1/49")
        self.assertTrue(vlti["rejected"])
        self.assertIn({"name": "VLT interconnect", "status": "fail", "detail": "VLT discovery (VLTi) interface"},
                      vlti["safety"]["checks"])

    def test_management_path(self):
        self.assertIn("is classified as on the management path (management IP is on in-band VLAN 10 and this port "
                      "carries VLAN 10)", self.reasons(["switchport trunk allowed vlan add 30"], "ethernet1/1/19", config=INBAND)[0])
        self.assertFalse(plan(["switchport access vlan 30"], config=INBAND)["rejected"])  # 1/1/18 carries VLAN 20 only
        unknown = self.reasons(["shutdown"], "ethernet1/1/18", management_ip="192.0.2.99")
        self.assertEqual(unknown, [f"shutdown rejected: {os10_safety.UNSAFE} (management path: management IP not found "
                                   f"in the running configuration)"])

    def test_explicit_protection_covers_every_change_and_fails_closed(self):
        reasons = self.reasons(["description X"], "ethernet1/1/40", protected={"ethernet1/1/40"})
        self.assertEqual(reasons, ["description change rejected: ethernet1/1/40 is classified as protected "
                                   "(listed in OS10_PROTECTED_INTERFACES)."])
        self.assertIn(os10_safety.UNSAFE, self.reasons(["description X"], "ethernet1/1/18", protected_error="malformed")[0])
        self.assertEqual(platforms.protected_interfaces("Kenda-Core-1", "Kenda-Core-1:ethernet1/1/40, *:Ethernet1/1/41,"
                                                                       "Kenda-Core-2:ethernet1/1/42"),
                         ({"ethernet1/1/40", "ethernet1/1/41"}, None))
        self.assertIsNotNone(platforms.protected_interfaces("Kenda-Core-1", "ethernet1/1/40")[1])
        self.assertIsNotNone(platforms.protected_interfaces("Kenda-Core-1", "Kenda-Core-1:eth1/1/40")[1])
        self.assertEqual(platforms.protected_interfaces("Kenda-Core-1", ""), (set(), None))

    def test_unknown_safety_data_rejects(self):
        for overrides, check in (({"lldp_neighbors": None, "lldp_error": "show lldp neighbors failed"}, "LLDP neighbor (live)"),
                                 ({"stored_links": None, "stored_error": "stored topology unavailable"},
                                  "LLDP neighbor (stored topology)")):
            with self.subTest(check=check):
                reasons = self.reasons(["shutdown"], "ethernet1/1/18", **overrides)
                self.assertTrue(reasons[0].startswith(f"shutdown rejected: {os10_safety.UNSAFE} ({check}:"), reasons)
        admin = self.reasons(["shutdown"], "ethernet1/1/18", config=AMBIGUOUS)
        self.assertIn("(admin state: both `shutdown` and `no shutdown` present)", admin[0])
        mode = self.reasons(["switchport access vlan 1"], "ethernet1/1/23", config=AMBIGUOUS)
        self.assertIn("(switchport state: unrecognised switchport line (switchport port-security))", mode[0])
        inband = self.reasons(["switchport access vlan 1"], "ethernet1/1/22",
                              config=AMBIGUOUS.replace("interface mgmt1/1/1\n no shutdown\n ip address 192.0.2.10/24",
                                                       "interface vlan1\n ip address 192.0.2.10/24"))
        self.assertIn("(management path: management IP is on in-band VLAN 1; this port's VLANs cannot be determined)",
                      inband[0])

    def test_description_only_keeps_v1_scope(self):
        result = plan(["description PEER-LINK"], "ethernet1/1/25", lldp_neighbors=None, lldp_error="not collected",
                      stored_links=None, stored_error="not collected")
        self.assertFalse(result["rejected"])  # descriptions are never disruptive; no LLDP read is needed
        self.assertEqual(result["device_commands"], ["description PEER-LINK"])

    def test_no_shutdown_rules(self):
        self.assertEqual(plan(["no shutdown"], "ethernet1/1/20")["device_commands"], ["no shutdown"])
        endpoint = [{"remote_system_name": "server-07", "remote_device_id": None, "source": "stored (recently seen)"}]
        self.assertFalse(plan(["no shutdown"], "ethernet1/1/20", stored_links=endpoint)["rejected"])
        infra = [{"remote_system_name": "Kenda-HQ-IDF-B", "remote_device_id": 8, "remote_hostname": "Kenda-HQ-IDF-B"}]
        self.assertIn("no shutdown rejected: ethernet1/1/20 is classified as an uplink",
                      plan(["no shutdown"], "ethernet1/1/20", stored_links=infra)["rejection_reasons"][0])
        # A shutdown, by contrast, is blocked by ANY neighbor.
        self.assertTrue(plan(["shutdown"], "ethernet1/1/18", stored_links=endpoint)["rejected"])

    def test_shutdown_on_confirmed_safe_port(self):
        result = plan(["shutdown"])
        self.assertEqual((result["rejected"], result["device_commands"]), (False, ["shutdown"]))


# ---- Phases 7, 8, 11: planner -------------------------------------------------------------------
class PlannerTests(unittest.TestCase):
    def test_access_vlan_change(self):
        result = plan(["switchport access vlan 30"])
        self.assertEqual(result["device_commands"], ["switchport access vlan 30"])
        self.assertEqual(result["proposed_changes"], ["switchport access vlan 30"])
        self.assertEqual(result["change_plan"], ["access VLAN: 20 → 30"])
        self.assertEqual(result["requested_state"], {"access_vlan": 30})
        self.assertEqual(result["current_state"], {
            "interface": "ethernet1/1/18", "description": None, "admin": "up", "mode": "access", "access_vlan": 20,
            "allowed_vlans": None, "port_channel": None, "lldp_neighbor": "none", "protected": False,
            "classification": "eligible"})
        self.assertEqual(result["expected_diff"]["current"][:3], ["interface ethernet1/1/18", " no shutdown",
                                                                  " switchport access vlan 20"])
        self.assertIn(" switchport access vlan 30", result["expected_diff"]["requested"])

    def test_trunk_conversion(self):
        result = plan(["switchport mode trunk", "switchport trunk allowed vlan 30,40"])
        self.assertEqual(result["device_commands"], ["switchport mode trunk", "switchport trunk allowed vlan 30,40"])
        self.assertEqual(result["change_plan"], [
            "mode: access → trunk", "untagged VLAN: 20 (kept: OS10 uses the access VLAN as the trunk's untagged VLAN)",
            "allowed VLANs: none → 30,40 (add 30,40)"])
        self.assertEqual(result["expected_diff"]["requested"][:5], [
            "interface ethernet1/1/18", " no shutdown", " switchport mode trunk", " switchport access vlan 20",
            " switchport trunk allowed vlan 30,40"])

    def test_untagged_vlan_cannot_also_be_tagged(self):
        result = plan(["switchport mode trunk", "switchport trunk allowed vlan 20,30,40"])
        self.assertEqual(result["rejection_reasons"], [
            "VLAN 20 is the untagged VLAN of ethernet1/1/18; it cannot also be allowed tagged. Changing the native "
            "VLAN is not supported in this release."])
        self.assertEqual((result["device_commands"], result["change_plan"], result["expected_diff"]["requested"]), ([], [], []))

    def test_allowed_vlan_set_add_remove(self):
        cases = {
            "switchport trunk allowed vlan 20,30": ["switchport trunk allowed vlan 20,30", "no switchport trunk allowed vlan 10"],
            "switchport trunk allowed vlan add 30-31": ["switchport trunk allowed vlan 10,20,30-31"],
            "switchport trunk allowed vlan remove 10": ["no switchport trunk allowed vlan 10"],
            "switchport trunk allowed vlan remove 10,20": ["no switchport trunk allowed vlan 10,20"],
        }
        config = L2.replace("interface vlan40\n", "interface vlan31\n no shutdown\n!\ninterface vlan40\n")
        for line, commands in cases.items():
            with self.subTest(line=line):
                self.assertEqual(plan([line], "ethernet1/1/19", config=config)["device_commands"], commands)
        self.assertEqual(plan(["switchport trunk allowed vlan 20,30"], "ethernet1/1/19")["change_plan"],
                         ["allowed VLANs: 10,20 → 20,30 (add 30, remove 10)"])

    def test_trunk_to_access(self):
        result = plan(["switchport mode access", "switchport access vlan 30"], "ethernet1/1/19")
        self.assertEqual(result["device_commands"], ["no switchport trunk allowed vlan 10,20", "switchport mode access",
                                                     "switchport access vlan 30"])
        kept = plan(["switchport mode access"], "ethernet1/1/19")
        self.assertEqual(kept["device_commands"], ["no switchport trunk allowed vlan 10,20", "switchport mode access"])
        self.assertIn("access VLAN: 1 (kept: the trunk's untagged VLAN becomes the access VLAN)", kept["change_plan"])

    def test_idempotency(self):
        for interface, lines in (("ethernet1/1/19", ["switchport mode trunk"]),
                                 ("ethernet1/1/19", ["switchport trunk allowed vlan 10,20"]),
                                 ("ethernet1/1/19", ["switchport trunk allowed vlan add 10"]),
                                 ("ethernet1/1/19", ["switchport trunk allowed vlan remove 30"]),
                                 ("ethernet1/1/18", ["switchport mode access", "switchport access vlan 20"]),
                                 ("ethernet1/1/18", ["no shutdown", "no description"]),
                                 ("ethernet1/1/20", ["shutdown", "description AUTOMATION-TEST"])):
            with self.subTest(lines=lines):
                result = plan(lines, interface)
                self.assertEqual((result["rejected"], result["would_change"], result["device_commands"]), (False, False, []))
                self.assertEqual(result["already_present"], check_os10(lines, [f"interface {interface}"])[0])
        partial = plan(["description X", "switchport access vlan 20"])
        self.assertEqual((partial["already_present"], partial["device_commands"]), (["switchport access vlan 20"], ["description X"]))

    def test_mode_conflicts_with_current_state(self):
        self.assertIn("is a trunk port; on OS10 `switchport access vlan` on a trunk sets its untagged (native) VLAN",
                      plan(["switchport access vlan 30"], "ethernet1/1/19")["rejection_reasons"][0])
        self.assertEqual(plan(["switchport trunk allowed vlan add 30"])["rejection_reasons"],
                         ["trunk allowed-VLAN change rejected: ethernet1/1/18 is an access port. "
                          "Add `switchport mode trunk` to convert it."])

    def test_nonexistent_vlans(self):
        self.assertEqual(plan(["switchport access vlan 200"])["rejection_reasons"], ["VLAN 200 does not exist on device."])
        self.assertEqual(plan(["switchport trunk allowed vlan add 200,300"], "ethernet1/1/19")["rejection_reasons"],
                         ["VLAN 200 does not exist on device.", "VLAN 300 does not exist on device."])
        many = plan(["switchport trunk allowed vlan add 100-120"], "ethernet1/1/19")["rejection_reasons"]
        self.assertEqual((len(many), many[-1]), (11, "... and 11 more VLANs do not exist on device."))
        # Removing a VLAN never requires it to exist.
        self.assertFalse(plan(["switchport trunk allowed vlan remove 200"], "ethernet1/1/19")["rejected"])

    def test_command_order_with_admin_state(self):
        result = plan(["no shutdown", "description EDGE", "switchport access vlan 30"], "ethernet1/1/20")
        self.assertEqual(result["device_commands"], ["description EDGE", "switchport access vlan 30", "no shutdown"])
        result = plan(["shutdown", "switchport mode trunk", "switchport trunk allowed vlan 30"])
        self.assertEqual(result["device_commands"], ["shutdown", "switchport mode trunk", "switchport trunk allowed vlan 30"])
        for lines in (["shutdown", "description EDGE"], ["switchport mode trunk", "switchport trunk allowed vlan 10,30"]):
            check_os10_commands(plan(lines)["device_commands"], ["interface ethernet1/1/18"])

    def test_command_results_per_requested_line(self):
        result = plan(["description X", "switchport access vlan 20"])
        self.assertEqual([(r["command"], r["desired_state_present"], r["type"]) for r in result["command_results"]],
                         [("description X", False, "interface_description"), ("switchport access vlan 20", True, "access_vlan")])


# ---- Phases 10, 17: post-check per command ------------------------------------------------------
class PostcheckTests(unittest.TestCase):
    P = ["interface ethernet1/1/18"]
    ACCESS = ["no shutdown", "switchport access vlan 20"]
    TRUNK = ["no shutdown", "switchport mode trunk", "switchport access vlan 1", "switchport trunk allowed vlan 10,20,30"]

    def check(self, command, body):
        return os10.verify([command], self.P, replace_block(L2, "ethernet1/1/18", body))

    def assertState(self, command, body, present):
        result = self.check(command, body)
        self.assertEqual((result["would_change"], result["command_results"][0]["desired_state_present"]),
                         (not present, present), (command, body))

    def assertAmbiguous(self, command, body):
        with self.assertRaises(os10.VerificationError, msg=(command, body)):
            self.check(command, body)

    def test_description(self):
        self.assertState("description X", ["description X"] + self.ACCESS, True)
        self.assertState("description X", ["description Y"] + self.ACCESS, False)
        self.assertState("description X", self.ACCESS, False)  # missing field = not applied
        self.assertState("no description", self.ACCESS, True)
        self.assertState("no description", ["description Y"] + self.ACCESS, False)

    def test_mode(self):
        self.assertState("switchport mode access", self.ACCESS, True)
        self.assertState("switchport mode access", self.TRUNK, False)
        self.assertState("switchport mode trunk", self.TRUNK, True)
        self.assertState("switchport mode trunk", self.ACCESS, False)
        self.assertAmbiguous("switchport mode trunk", ["no shutdown"])  # mode not shown
        self.assertAmbiguous("switchport mode access", ["no shutdown", "switchport mode access", "switchport mode trunk"])

    def test_access_vlan(self):
        self.assertState("switchport access vlan 20", self.ACCESS, True)
        self.assertState("switchport access vlan 30", self.ACCESS, False)
        self.assertState("switchport access vlan 1", self.TRUNK, False)  # untagged VLAN of a trunk is not an access VLAN
        self.assertAmbiguous("switchport access vlan 20", ["no shutdown"])
        self.assertAmbiguous("switchport access vlan 20", ["no shutdown", "switchport access vlan 20", "switchport access vlan 30"])

    def test_trunk_allowed(self):
        self.assertState("switchport trunk allowed vlan 10,20", self.TRUNK, True)
        self.assertState("switchport trunk allowed vlan 10,40", self.TRUNK, False)
        self.assertState("switchport trunk allowed vlan 10", self.TRUNK[:3], False)  # no allowed line
        self.assertState("switchport trunk allowed vlan 10", self.ACCESS, False)
        self.assertState("no switchport trunk allowed vlan 40", self.TRUNK, True)
        self.assertState("no switchport trunk allowed vlan 10", self.TRUNK, False)
        self.assertState("no switchport trunk allowed vlan 10", self.ACCESS, True)
        self.assertAmbiguous("switchport trunk allowed vlan 10", self.TRUNK[:3] + ["switchport trunk allowed vlan 10,x"])
        self.assertAmbiguous("no switchport trunk allowed vlan 10", ["no shutdown", "switchport trunk allowed vlan 10"])

    def test_admin_state(self):
        self.assertState("shutdown", ["shutdown", "switchport access vlan 20"], True)
        self.assertState("shutdown", self.ACCESS, False)
        self.assertState("no shutdown", self.ACCESS, True)
        self.assertState("no shutdown", ["shutdown", "switchport access vlan 20"], False)
        for command in ("shutdown", "no shutdown"):
            self.assertAmbiguous(command, ["switchport access vlan 20"])
            self.assertAmbiguous(command, ["shutdown", "no shutdown", "switchport access vlan 20"])

    def test_only_relevant_fields_must_be_unambiguous(self):
        self.assertState("description X", ["description X", "shutdown", "no shutdown"], True)

    def test_missing_interface_and_non_canonical_command(self):
        with self.assertRaisesRegex(os10.VerificationError, "not found"):
            os10.verify(["shutdown"], ["interface ethernet1/1/99"], L2)
        with self.assertRaisesRegex(os10.VerificationError, "not a canonical device command"):
            os10.verify(["switchport trunk allowed vlan add 10"], self.P, L2)


# ---- Phase 18: workflow flows with the simulated device model in-process -----------------------
INVENTORY = [{"id": 12, "hostname": "Kenda-Core-1", "management_ip": MGMT_IP, "platform": "dell_os10"},
             {"id": 13, "hostname": "Kenda-Core-2", "management_ip": "192.0.2.20", "platform": "dell_os10"}]
IDENTITIES = [{"device_id": 13, "lldp_system_name": "kenda-core-02", "chassis_id": "e8:b5:d0:7a:5c:a3"}]


class L2WorkflowTests(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        self.device = FakeOS10()
        self.addCleanup(self.device.close)
        self.reads = 0
        cls = platforms.Os10Platform
        self.read.side_effect = lambda host: self._read()["running_config"]
        mock.patch.object(cls, "read_device_state", side_effect=lambda host, lines: self._read(lines)).start()
        self.write.side_effect = self._write
        mock.patch("db.topology.list_inventory", return_value=INVENTORY).start()
        mock.patch("db.topology.list_lldp_identities", return_value=IDENTITIES).start()
        self.stored = mock.patch("db.topology.list_links_for_interface", return_value=[]).start()
        self.sent = []

    def _read(self, lines=()):
        self.reads += 1
        lldp = change_kinds(lines) - {"description"}
        return {"running_config": self.device.running_config(), "lldp": self.device.lldp_neighbors() if lldp else None,
                "lldp_error": None if lldp else "not collected"}

    def _write(self, host, lines, parents):
        self.sent.append(list(lines))
        mode = f"if:{parents[0].split()[1]}"
        for line in lines:
            error = self.device._handle(mode, line)[1]
            if error:
                return {"returncode": 2, "stdout": f"failed: [{host}] (item={line}) => {{\"msg\": \"{error}\"}}", "stderr": ""}
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    def precheck(self, lines, interface="ethernet1/1/18"):
        return workflow.run_precheck("Kenda-Core-1", lines, [f"interface {interface}"], backup=self.backup)

    def approve_and_apply(self, result, status="applying"):
        self.assertEqual(result["status"], "pending_approval")
        stored = self.create_approval.call_args.kwargs
        self.approval(status=status, lines=stored["config_lines"], parents=stored["config_parents"])
        return workflow.run_apply(APPROVAL, claimed_by_api=True)

    def test_a_access_vlan_change(self):
        result = self.precheck(["switchport access vlan 30"])
        self.assertEqual(result["status"], "pending_approval")
        self.backup.assert_called_once_with("Kenda-Core-1", config_snapshot=mock.ANY)
        stored = self.create_approval.call_args.kwargs
        self.assertEqual((stored["config_lines"], stored["config_parents"], stored["backup_job_id"]),
                         (["switchport access vlan 30"], ["interface ethernet1/1/18"], BACKUP_JOB))
        self.assertEqual(self.sent, [])  # precheck never writes
        applied = self.approve_and_apply(result)
        self.assertEqual((applied["status"], applied["sent_lines"]), ("applied", ["switchport access vlan 30"]))
        self.assertEqual(self.device.interfaces["ethernet1/1/18"]["access_vlan"], 30)
        self.assertTrue(applied["post_check"]["command_results"][0]["desired_state_present"])
        self.assertEqual(self.events(), ["precheck_completed", "apply_started", "apply_completed"])

    def test_b_trunk_conversion(self):
        result = self.precheck(["switchport mode trunk", "switchport trunk allowed vlan 30,40"])
        self.assertEqual(result["dry_run"]["change_plan"][0], "mode: access → trunk")
        self.approve_and_apply(result)
        port = self.device.interfaces["ethernet1/1/18"]
        self.assertEqual((port["mode"], port["allowed"], port["access_vlan"]), ("trunk", {30, 40}, 20))
        self.assertEqual(self.sent, [["switchport mode trunk", "switchport trunk allowed vlan 30,40"]])

    def test_c_allowed_vlan_update(self):
        result = self.precheck(["switchport trunk allowed vlan 20,30"], "ethernet1/1/19")
        self.approve_and_apply(result)
        self.assertEqual(self.device.interfaces["ethernet1/1/19"]["allowed"], {20, 30})
        self.assertEqual(self.sent, [["switchport trunk allowed vlan 20,30", "no switchport trunk allowed vlan 10"]])

    def test_d_description_change(self):
        result = self.precheck(["description EDGE-18"])
        self.approve_and_apply(result)
        self.assertEqual(self.device.interfaces["ethernet1/1/18"]["description"], "EDGE-18")

    def test_e_shutdown_safe_port(self):
        result = self.precheck(["shutdown"])
        self.assertEqual(result["dry_run"]["safety"]["status"], "PASS")
        self.approve_and_apply(result)
        self.assertEqual(self.device.interfaces["ethernet1/1/18"]["admin"], "down")

    def test_f_shutdown_on_uplink_is_blocked(self):
        result = self.precheck(["shutdown"], "ethernet1/1/25")
        self.assertEqual((result["status"], result["ready_for_approval"]), ("rejected", False))
        self.assertEqual(result["rejection_reasons"], [
            "shutdown rejected: ethernet1/1/25 is classified as an uplink (live LLDP: kenda-core-02 ethernet1/1/25 = "
            "inventory device Kenda-Core-2)."])
        self.backup.assert_not_called()
        self.create_approval.assert_not_called()
        self.assertEqual(self.events(), ["precheck_failed"])
        self.assertIn("rejected by safety policy: shutdown rejected", self.audit.call_args.kwargs["message"])

    def test_g_nonexistent_vlan(self):
        result = self.precheck(["switchport access vlan 200"])
        self.assertEqual((result["status"], result["rejection_reasons"]), ("rejected", ["VLAN 200 does not exist on device."]))
        self.backup.assert_not_called()
        self.create_approval.assert_not_called()

    def test_h_postcheck_mismatch(self):
        result = self.precheck(["switchport access vlan 30"])
        self.device.ignore_writes = True
        with self.assertRaisesRegex(workflow.ChangeError, r"Post-check failed .*\(missing: switchport access vlan 30\)"):
            self.approve_and_apply(result)
        self.failed.assert_called_once_with(APPROVAL)
        self.applied.assert_not_called()

    def test_i_cancellation_before_apply(self):
        result = self.precheck(["shutdown"])
        with self.assertRaisesRegex(ValueError, "is cancelled, expected applying"):
            self.approve_and_apply(result, status="cancelled")
        self.assertEqual((self.sent, self.device.interfaces["ethernet1/1/18"]["admin"]), ([], "up"))

    def test_j_duplicate_apply(self):
        result = self.precheck(["shutdown"])
        self.approve_and_apply(result)
        with self.assertRaisesRegex(ValueError, "is applied, expected applying"):
            self.approve_and_apply(result, status="applied")
        self.assertEqual(len(self.sent), 1)

    def test_state_drift_neighbor_appears_before_apply(self):
        result = self.precheck(["shutdown"])
        self.device.lldp["ethernet1/1/18"] = ("Kenda-HQ-IDF-B", "Te1/0/4", "e8:b5:d0:cd:0c:cb")
        with self.assertRaisesRegex(workflow.ChangeError, "^Pre-apply safety check failed; nothing was sent: shutdown "
                                                          "rejected: ethernet1/1/18 is classified as an LLDP-connected port"):
            self.approve_and_apply(result)
        self.assertEqual(self.sent, [])
        self.failed.assert_called_once()

    def test_state_drift_port_became_trunk_before_access_vlan_apply(self):
        result = self.precheck(["switchport access vlan 30"])
        self.device.interfaces["ethernet1/1/18"].update(mode="trunk", allowed={10})
        with self.assertRaisesRegex(workflow.ChangeError, "nothing was sent: ethernet1/1/18 is no longer an access port"):
            self.approve_and_apply(result)
        self.assertEqual(self.sent, [])

    def test_only_still_needed_commands_are_sent(self):
        result = self.precheck(["description PARKED", "shutdown"])
        self.device.interfaces["ethernet1/1/18"]["description"] = "PARKED"  # someone already did half of it
        applied = self.approve_and_apply(result)
        self.assertEqual(self.sent, [["shutdown"]])
        self.assertEqual(applied["post_check"]["already_present"], ["shutdown", "description PARKED"])

    def test_already_applied_sends_nothing(self):
        result = self.precheck(["switchport access vlan 30"])
        self.device.interfaces["ethernet1/1/18"]["access_vlan"] = 30
        applied = self.approve_and_apply(result)
        self.assertTrue(applied["write_skipped"])
        self.assertEqual(self.sent, [])

    def test_ambiguous_state_at_postcheck_fails_safely(self):
        result = self.precheck(["shutdown"])
        original = self.device.running_config

        def broken():  # after the write, the device shows both admin lines
            text = original()
            return text.replace(" shutdown\r\n switchport access vlan 20", " shutdown\r\n no shutdown\r\n switchport access vlan 20") \
                if self.sent else text
        self.device.running_config = broken
        with self.assertRaisesRegex(workflow.ChangeError, "may have been applied - verify manually: cannot determine admin state"):
            self.approve_and_apply(result)
        self.failed.assert_called_once()

    def test_lldp_read_failure_rejects_l2_but_not_description(self):
        def no_lldp(host, lines):
            return {**self._read(lines), "lldp": None, "lldp_error": "show lldp neighbors failed"}
        platforms.Os10Platform.read_device_state.side_effect = no_lldp
        self.assertEqual(self.precheck(["switchport access vlan 30"])["status"], "rejected")
        self.assertEqual(self.precheck(["description OK"])["status"], "pending_approval")


if __name__ == "__main__":
    unittest.main()
