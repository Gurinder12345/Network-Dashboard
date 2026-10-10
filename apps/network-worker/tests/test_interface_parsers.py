"""
Interface parsers (OS6 / OS10), name normalization and the fixed-width table reader.
Fixtures under tests/fixtures/interfaces/ are SYNTHETIC (documented layouts); any
os6_real_* / os10_real_* capture placed there is parsed too. Run inside the worker image:
    python -m unittest tests.test_interface_parsers -v
"""

import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interfaces import parsers  # noqa: E402
from interfaces.names import canonical_name, interface_type  # noqa: E402
from interfaces.parsers import (  # noqa: E402
    INTERFACE_COMMANDS, OS6_CONFIGURATION, OS6_COUNTERS, OS6_ERRORS, OS6_STATUS, OS10_INTERFACE, OS10_STATUS,
    InterfaceParseError, age_seconds, parse_interfaces, speed_bps,
)
from interfaces.tables import parse_tables  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "interfaces")


def read(path):
    with open(path) as handle:
        return handle.read()


def load(platform):
    prefix = "os6" if platform == "dell_os6" else "os10"
    return {c: read(os.path.join(FIXTURES, f"{prefix}_{c.replace(' ', '_')}.txt")) for c in INTERFACE_COMMANDS[platform]}


def by_name(result):
    return {i["canonical_name"]: i for i in result["interfaces"]}


class NameTests(unittest.TestCase):
    def test_os6_short_and_long_forms_agree(self):
        for name in ("Te1/0/1", "TenGigabitEthernet1/0/1", "te1/0/1", "Te 1/0/1"):
            self.assertEqual(canonical_name("dell_os6", name), "te1/0/1")
        self.assertEqual(canonical_name("dell_os6", "Tw1/0/4"), "tw1/0/4")
        self.assertEqual(canonical_name("dell_os6", "TwentyFiveGigE1/0/4"), "tw1/0/4")
        self.assertEqual(canonical_name("dell_os6", "Port-channel 1"), "po1")

    def test_os10_forms_agree(self):
        for name in ("Eth 1/1/1", "Ethernet 1/1/1", "ethernet1/1/1"):
            self.assertEqual(canonical_name("dell_os10", name), "ethernet1/1/1")
        self.assertEqual(canonical_name("dell_os10", "Eth 1/1/26:1"), "ethernet1/1/26:1")
        for name in ("Po 10", "Port-channel 10", "port-channel10"):
            self.assertEqual(canonical_name("dell_os10", name), "port-channel10")
        self.assertEqual(canonical_name("dell_os10", "Management 1/1/1"), "mgmt1/1/1")

    def test_unknown_names_are_not_merged(self):
        # Different interfaces stay different; nothing is guessed from a partial prefix.
        self.assertNotEqual(canonical_name("dell_os6", "Te1/0/1"), canonical_name("dell_os6", "Tw1/0/1"))
        self.assertNotEqual(canonical_name("dell_os10", "ethernet1/1/26:1"), canonical_name("dell_os10", "ethernet1/1/26"))
        self.assertEqual(canonical_name("dell_os10", "Foo 9"), "foo9")
        self.assertIsNone(canonical_name("dell_os6", "  "))

    def test_types(self):
        self.assertEqual(interface_type("dell_os6", "po1"), "port_channel")
        self.assertEqual(interface_type("dell_os6", "gi1/0/1"), "ethernet")
        self.assertEqual(interface_type("dell_os10", "mgmt1/1/1"), "management")
        self.assertEqual(interface_type("dell_os10", "vlan10"), "other")


class TableTests(unittest.TestCase):
    def test_two_line_header_and_dash_groups(self):
        tables = parse_tables(load("dell_os6")[OS6_STATUS])
        self.assertIn("linkstate", tables[0]["columns"])
        self.assertEqual(tables[0]["rows"][0]["port"], "Gi1/0/1")
        self.assertEqual(tables[0]["rows"][1]["description"], "")  # empty cell stays empty

    def test_full_width_rule_header(self):
        tables = parse_tables(load("dell_os10")[OS10_STATUS])
        self.assertEqual(tables[0]["columns"][:3], ["port", "description", "status"])
        self.assertEqual(tables[0]["rows"][0]["port"], "Eth 1/1/5")  # space inside a cell

    def test_values(self):
        self.assertEqual(speed_bps("10000"), 10_000_000_000)  # OS6 Mb/s
        self.assertEqual(speed_bps("10G"), 10_000_000_000)
        self.assertEqual(speed_bps("1000M"), 1_000_000_000)
        for unknown in ("Unknown", "N/A", "0", "auto", "", None):
            self.assertIsNone(speed_bps(unknown))
        self.assertEqual(age_seconds("1 weeks 1 days 03:02:50"), 702170)
        self.assertEqual(age_seconds("00:12:30"), 750)
        self.assertIsNone(age_seconds("never"))
        self.assertIsNone(parsers.count("99999999999999999999999"))  # beyond bigint -> misparse, not stored


class Os6Tests(unittest.TestCase):
    def setUp(self):
        self.outputs = load("dell_os6")

    def test_normal(self):
        result = parse_interfaces("dell_os6", self.outputs)
        self.assertEqual(result["status"], "success")
        ports = by_name(result)
        self.assertEqual(set(ports), {"gi1/0/1", "gi1/0/2", "gi1/0/3", "te1/0/1", "te1/0/2", "tw1/0/4", "gi1/0/5", "po1"})
        gi = ports["gi1/0/1"]
        self.assertEqual((gi["admin_status"], gi["oper_status"], gi["speed_bps"], gi["duplex"]), ("up", "up", 10**9, "full"))
        self.assertEqual((gi["mode"], gi["access_vlan"], gi["mtu"]), ("access", 10, 1518))
        self.assertEqual((gi["rx_bytes"], gi["tx_bytes"], gi["rx_packets"]), (812345678, 406172839, 900000 + 1200 + 340))

    def test_categories(self):
        ports = by_name(parse_interfaces("dell_os6", self.outputs))
        self.assertIsNone(ports["gi1/0/2"]["description"])                                           # missing description
        self.assertEqual((ports["gi1/0/2"]["admin_status"], ports["gi1/0/2"]["oper_status"]), ("up", "down"))  # oper down
        self.assertIsNone(ports["gi1/0/2"]["speed_bps"])                                             # "Unknown" speed, not 0
        self.assertEqual(ports["gi1/0/3"]["admin_status"], "down")                                   # admin down
        self.assertEqual(ports["te1/0/1"]["mode"], "trunk")                                          # trunk
        self.assertEqual(ports["gi1/0/5"]["mode"], "general")
        self.assertEqual(ports["po1"]["interface_type"], "port_channel")                            # port-channel
        self.assertEqual(ports["te1/0/1"]["crc_errors"], 17)
        self.assertEqual(ports["gi1/0/5"]["output_discards"], 12)

    def test_missing_optional_commands_give_partial_data(self):
        for missing in (OS6_CONFIGURATION, OS6_COUNTERS, OS6_ERRORS):
            outputs = {k: v for k, v in self.outputs.items() if k != missing}
            result = parse_interfaces("dell_os6", outputs)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(len(result["interfaces"]), 8)
            self.assertTrue(any(missing in p for p in result["problems"]))
        result = parse_interfaces("dell_os6", {**self.outputs, OS6_COUNTERS: "% Invalid input detected at '^' marker."})
        self.assertIsNone(by_name(result)["gi1/0/1"]["rx_bytes"])  # unsupported -> None, never 0
        self.assertEqual(by_name(result)["gi1/0/1"]["admin_status"], "up")

    def test_required_command_failure_and_malformed_output(self):
        with self.assertRaises(InterfaceParseError):
            parse_interfaces("dell_os6", {k: v for k, v in self.outputs.items() if k != OS6_STATUS})
        with self.assertRaises(InterfaceParseError):
            parse_interfaces("dell_os6", {**self.outputs, OS6_STATUS: "garbage\nmore garbage\n"})
        with self.assertRaises(InterfaceParseError):
            parse_interfaces("dell_os6", {**self.outputs, OS6_STATUS: "% Invalid input detected at '^' marker."})
        # Malformed counters: non-numeric cells stay None for those fields only.
        broken = self.outputs[OS6_COUNTERS].replace("812345678", "8x2345678")
        self.assertIsNone(by_name(parse_interfaces("dell_os6", {**self.outputs, OS6_COUNTERS: broken}))["gi1/0/1"]["rx_bytes"])


def load_real_os6():
    return {c: read(os.path.join(FIXTURES, f"os6_real_{c.replace(' ', '_')}.txt")) for c in INTERFACE_COMMANDS["dell_os6"]}


class RealOs6Tests(unittest.TestCase):
    """Regression tests on the REAL Kenda-HARO-SW-01 capture (N-series 6.x)."""

    @classmethod
    def setUpClass(cls):
        cls.result = parse_interfaces("dell_os6", load_real_os6())
        cls.ports = by_name(cls.result)

    def test_28_interfaces_no_join_errors(self):
        self.assertEqual(self.result["status"], "success")
        self.assertEqual(self.result["problems"], [])
        self.assertEqual(len(self.result["interfaces"]), 28)
        expected = {f"gi1/0/{n}" for n in range(1, 25)} | {f"te1/0/{n}" for n in range(1, 5)}
        self.assertEqual(set(self.ports), expected)  # Po1-Po64 placeholders (not in status) are not inventory
        # Every port joined with all four commands: configuration (admin/MTU), counters, errors.
        for name, item in self.ports.items():
            self.assertEqual(item["admin_status"], "up", name)
            self.assertEqual(item["mtu"], 1518, name)
            self.assertIsNotNone(item["rx_packets"], name)
            self.assertIsNotNone(item["crc_errors"], name)

    def test_packet_counters_parse_and_bytes_stay_null(self):
        gi1 = self.ports["gi1/0/1"]
        self.assertEqual((gi1["rx_packets"], gi1["tx_packets"]), (2382916330, 3685701163))
        te1 = self.ports["te1/0/1"]
        self.assertEqual((te1["rx_packets"], te1["tx_packets"]), (7797518408, 4652577882))
        self.assertEqual((self.ports["gi1/0/4"]["rx_packets"], self.ports["gi1/0/4"]["tx_packets"]), (0, 0))  # a real zero
        for name, item in self.ports.items():
            # The switch reports no octet counters for physical ports: None, never derived from packets.
            self.assertIsNone(item["rx_bytes"], name)
            self.assertIsNone(item["tx_bytes"], name)

    def test_error_counters(self):
        gi18 = self.ports["gi1/0/18"]
        self.assertEqual((gi18["crc_errors"], gi18["rx_errors"], gi18["tx_errors"], gi18["output_discards"]), (8, 9, 0, 27954))
        self.assertEqual(self.ports["te1/0/3"]["crc_errors"], 2)
        self.assertEqual(self.ports["gi1/0/1"]["output_discards"], 4847244)
        for item in self.ports.values():
            self.assertIsNone(item["input_discards"])  # not reported by this command: None, not 0

    def test_status_admin_oper_speed_duplex(self):
        gi1, gi3, te3, te4 = (self.ports[n] for n in ("gi1/0/1", "gi1/0/3", "te1/0/3", "te1/0/4"))
        self.assertEqual((gi1["oper_status"], gi1["speed_bps"], gi1["duplex"]), ("up", 10**9, "full"))
        self.assertEqual((gi3["oper_status"], gi3["speed_bps"], gi3["duplex"]), ("down", None, None))  # "Unknown" / "N/A"
        self.assertEqual((te3["oper_status"], te3["speed_bps"]), ("up", 10**10))
        self.assertEqual((te4["oper_status"], te4["speed_bps"]), ("down", 10**9))
        self.assertEqual(self.ports["gi1/0/6"]["speed_bps"], 10**8)
        statuses = [i["oper_status"] for i in self.ports.values()]
        self.assertEqual((statuses.count("up"), statuses.count("down")), (16, 12))

    def test_access_vlans(self):
        self.assertEqual((self.ports["gi1/0/2"]["mode"], self.ports["gi1/0/2"]["access_vlan"]), ("access", 10))
        self.assertEqual((self.ports["gi1/0/3"]["mode"], self.ports["gi1/0/3"]["access_vlan"]), ("access", 66))
        access = [n for n, i in self.ports.items() if i["mode"] == "access"]
        self.assertEqual(len(access), 23)
        for name in access:
            self.assertIsNone(self.ports[name]["native_vlan"])
            self.assertIsNone(self.ports[name]["allowed_vlans"])

    def test_trunks_only_where_the_switch_says_so(self):
        trunks = {n: (i["native_vlan"], i["allowed_vlans"]) for n, i in self.ports.items() if i["mode"] == "trunk"}
        self.assertEqual(trunks, {
            "gi1/0/1": (10, "1-9,11-4096"),   # M = T: a Gigabit port is a trunk too (not inferred from the name)
            "te1/0/1": (10, "1-9,11-4096"),
            "te1/0/2": (10, "1-9,11-4096"),
            "te1/0/3": (1, "2-4096"),
            "te1/0/4": (1, "2-4096"),
        })
        for name in trunks:
            self.assertIsNone(self.ports[name]["access_vlan"])

    def test_full_descriptions_from_configuration(self):
        self.assertEqual(self.ports["gi1/0/1"]["description"], "InterConnect_HARO_SW_1/2")  # status: "InterConnect_HA"
        self.assertEqual(self.ports["te1/0/1"]["description"], "UPLINK_HARO_HQ")
        self.assertIsNone(self.ports["gi1/0/2"]["description"])

    def test_mode_comes_only_from_evidence(self):
        status = load_real_os6()[OS6_STATUS]
        # Remove the M value of Gi1/0/1: its mode becomes unknown, never guessed from speed/name/VLAN list.
        line = next(l for l in status.splitlines() if l.startswith("Gi1/0/1 "))
        mode_at = status.splitlines()[1].index(" M ") + 1  # the "M" column of the real header
        self.assertEqual(line[mode_at], "T")
        status = status.replace(line, line[:mode_at] + " " + line[mode_at + 1:])
        ports = by_name(parse_interfaces("dell_os6", {**load_real_os6(), OS6_STATUS: status}))
        self.assertIsNone(ports["gi1/0/1"]["mode"])
        self.assertEqual(ports["te1/0/1"]["mode"], "trunk")

    def test_configured_lag_in_real_layout_is_inventoried(self):
        outputs = load_real_os6()
        # Columns of the real "Port / Channel" status table: 0, 8, 39, 47, 50.
        lag = "Po1".ljust(8) + "UPLINK-LAG".ljust(31) + "Up".ljust(8) + "T".ljust(3) + "(1),2-100"
        outputs[OS6_STATUS] = outputs[OS6_STATUS].rstrip("\n") + "\n" + lag + "\n"
        zero = "Po1".ljust(10) + " ".join("0".rjust(16) for _ in range(4))
        self.assertIn(zero, outputs[OS6_COUNTERS])
        values = "Po1".ljust(10) + " ".join(v.rjust(16) for v in ("1234567890", "9000", "100", "10"))
        counters = outputs[OS6_COUNTERS].replace(zero, values, 1)  # first match = the InOctets table
        ports = by_name(parse_interfaces("dell_os6", {**outputs, OS6_COUNTERS: counters}))
        po1 = ports["po1"]
        self.assertEqual((po1["interface_type"], po1["oper_status"], po1["mode"], po1["native_vlan"], po1["allowed_vlans"]),
                         ("port_channel", "up", "trunk", 1, "2-100"))
        self.assertEqual(po1["rx_bytes"], 1234567890)  # octets exist for port-channels in this command
        self.assertEqual(len(ports), 29)


class Os10Tests(unittest.TestCase):
    def setUp(self):
        self.outputs = load("dell_os10")

    def test_normal(self):
        result = parse_interfaces("dell_os10", self.outputs)
        self.assertEqual(result["status"], "success")
        ports = by_name(result)
        self.assertNotIn("vlan1", ports)  # SVIs are not monitored
        eth = ports["ethernet1/1/5"]
        self.assertEqual(eth["interface_name"], "Ethernet 1/1/5")
        self.assertEqual((eth["admin_status"], eth["oper_status"], eth["speed_bps"], eth["mtu"]), ("up", "up", 10**10, 9216))
        self.assertEqual((eth["rx_packets"], eth["rx_bytes"], eth["tx_packets"], eth["tx_bytes"]), (7154, 595610, 8476, 2024142))
        self.assertEqual((eth["mode"], eth["access_vlan"], eth["duplex"]), ("access", 20, "full"))
        self.assertEqual(eth["state_change_age_seconds"], 702170)

    def test_categories(self):
        ports = by_name(parse_interfaces("dell_os10", self.outputs))
        self.assertIsNone(ports["ethernet1/1/18"]["description"])                         # empty description line
        self.assertIsNone(ports["ethernet1/1/26:1"]["description"])                       # no description line
        self.assertEqual(ports["ethernet1/1/18"]["oper_status"], "down")                  # oper down
        self.assertEqual(ports["ethernet1/1/31"]["admin_status"], "down")                 # admin down
        trunk = ports["ethernet1/1/25"]
        self.assertEqual((trunk["mode"], trunk["native_vlan"], trunk["allowed_vlans"]), ("trunk", 1, "10,20,30"))
        self.assertEqual((trunk["crc_errors"], trunk["input_discards"], trunk["output_discards"]), (4, 2, 9))
        self.assertEqual(ports["port-channel10"]["interface_type"], "port_channel")      # port-channel
        self.assertEqual(ports["ethernet1/1/30:2"]["port_channel"], "Port-channel 10")    # LAG member
        self.assertEqual(ports["mgmt1/1/1"]["interface_type"], "management")

    def test_missing_optional_command(self):
        result = parse_interfaces("dell_os10", {OS10_INTERFACE: self.outputs[OS10_INTERFACE]})
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(by_name(result)["ethernet1/1/25"]["mode"])
        # Only the status table: inventory + oper state, no counters.
        result = parse_interfaces("dell_os10", {OS10_INTERFACE: "% Error: Unrecognized command.",
                                                OS10_STATUS: self.outputs[OS10_STATUS]})
        self.assertEqual(result["status"], "partial")
        eth = by_name(result)["ethernet1/1/5"]
        self.assertEqual((eth["oper_status"], eth["rx_bytes"], eth["admin_status"]), ("up", None, None))

    def test_malformed(self):
        with self.assertRaises(InterfaceParseError):
            parse_interfaces("dell_os10", {OS10_INTERFACE: "nothing useful", OS10_STATUS: "still nothing"})
        with self.assertRaises(InterfaceParseError):
            parse_interfaces("dell_os10", {})
        with self.assertRaises(InterfaceParseError):
            parse_interfaces("dell_os9", self.outputs)


class SafetyTests(unittest.TestCase):
    def test_collection_commands_are_show_only(self):
        for commands in INTERFACE_COMMANDS.values():
            for command in commands:
                self.assertTrue(parsers.SAFE_COMMAND.match(command), command)
                self.assertNotIn("clear", command)
                self.assertNotIn("|", command)
        self.assertEqual(len(INTERFACE_COMMANDS["dell_os6"]), 4)
        self.assertEqual(len(INTERFACE_COMMANDS["dell_os10"]), 2)


class RealCaptureTests(unittest.TestCase):
    """Parses any real capture added to fixtures/interfaces (os6_real_*, os10_real_*)."""

    def test_real_captures_parse(self):
        for platform, prefix in (("dell_os6", "os6"), ("dell_os10", "os10")):
            files = glob.glob(os.path.join(FIXTURES, f"{prefix}_real_*.txt"))
            if not files:
                continue
            outputs = {}
            for command in INTERFACE_COMMANDS[platform]:
                path = os.path.join(FIXTURES, f"{prefix}_real_{command.replace(' ', '_')}.txt")
                if os.path.exists(path):
                    outputs[command] = read(path)
            result = parse_interfaces(platform, outputs)
            self.assertTrue(result["interfaces"], platform)


if __name__ == "__main__":
    unittest.main()
