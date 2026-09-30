"""
LLDP parser / correlation tests. Run from apps/network-worker:

    python -m unittest discover -s tests -v

Fixtures in tests/fixtures are SYNTHETIC unless named *_real_*.txt (see fixtures/README.md).
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from topology.correlate import correlate  # noqa: E402
from topology.lldp import (  # noqa: E402
    LLDP_COMMANDS,
    count_table_rows,
    LldpParseError,
    lldp_command_for,
    parse_lldp_output,
    parse_os6_lldp_remote_device_all,
    parse_os10_lldp_neighbors,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

INVENTORY = [
    {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "10.0.0.10", "platform": "dell_os10"},
    {"id": 13, "hostname": "Kenda-Core-2", "management_ip": "10.0.0.20", "platform": "dell_os10"},
    {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "10.0.0.31", "platform": "dell_os6"},
    {"id": 20, "hostname": "Kenda-HQ-IDF-A", "management_ip": "10.0.0.29", "platform": "dell_os6"},
]


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return handle.read()


class CommandMappingTests(unittest.TestCase):
    def test_exact_commands(self):
        self.assertEqual(LLDP_COMMANDS, {
            "dell_os10": "show lldp neighbors",
            "dell_os6": "show lldp remote-device all",
        })
        self.assertEqual(lldp_command_for("dell_os10"), "show lldp neighbors")
        self.assertEqual(lldp_command_for("dell_os6"), "show lldp remote-device all")

    def test_unknown_platform(self):
        with self.assertRaises(LldpParseError):
            lldp_command_for("cisco_ios")


class Os10ParserTests(unittest.TestCase):
    def setUp(self):
        self.neighbors, self.warnings = parse_os10_lldp_neighbors(fixture("os10_multiple.txt"))

    def test_multiple_neighbors(self):
        self.assertEqual(len(self.neighbors), 5)
        self.assertEqual(self.warnings, [])

    def test_managed_neighbor_fields(self):
        n = self.neighbors[0]
        self.assertEqual(n["local_interface"], "ethernet1/1/1")
        self.assertEqual(n["remote_system_name"], "Kenda-HARO-IDF-A")
        self.assertEqual(n["remote_port_id"], "Te1/0/48")
        self.assertEqual(n["remote_interface"], "Te1/0/48")
        self.assertEqual(n["remote_chassis_id"], "f8:b1:56:aa:bb:01")
        self.assertEqual(n["protocol"], "lldp")

    def test_never_fabricates_ip_or_capabilities(self):
        for n in self.neighbors:
            self.assertIsNone(n["remote_management_ip"])
            self.assertIsNone(n["capabilities"])

    def test_mac_port_id_is_not_an_interface(self):
        ap = self.neighbors[3]
        self.assertEqual(ap["remote_system_name"], "unmanaged-ap-01")
        self.assertEqual(ap["remote_port_id"], "c0:ff:ee:00:00:20")
        self.assertIsNone(ap["remote_interface"])

    def test_missing_system_name_is_null(self):
        mgmt = self.neighbors[4]
        self.assertEqual(mgmt["local_interface"], "mgmt1/1/1")
        self.assertIsNone(mgmt["remote_system_name"])
        self.assertEqual(mgmt["remote_port_id"], "Gi1/0/5")

    def test_no_neighbors(self):
        neighbors, warnings = parse_os10_lldp_neighbors(fixture("os10_no_neighbors.txt"))
        self.assertEqual(neighbors, [])

    def test_truncated_output_fails(self):
        with self.assertRaises(LldpParseError):
            parse_os10_lldp_neighbors(fixture("os10_truncated.txt"))

    def test_cli_error_fails(self):
        with self.assertRaises(LldpParseError):
            parse_os10_lldp_neighbors(fixture("os10_error.txt"))

    def test_empty_output_fails(self):
        with self.assertRaises(LldpParseError):
            parse_os10_lldp_neighbors("")


class Os6ParserTests(unittest.TestCase):
    def setUp(self):
        self.neighbors, self.warnings = parse_os6_lldp_remote_device_all(fixture("os6_multiple.txt"))

    def test_multiple_neighbors_including_same_port(self):
        self.assertEqual(len(self.neighbors), 4)
        self.assertEqual(self.warnings, [])
        same_port = [n for n in self.neighbors if n["local_interface"] == "Gi1/0/12"]
        self.assertEqual(len(same_port), 2)

    def test_uplink_to_core(self):
        n = self.neighbors[0]
        self.assertEqual(n["local_interface"], "Te1/0/48")
        self.assertEqual(n["remote_system_name"], "Kenda-Core-1")
        self.assertEqual(n["remote_interface"], "ethernet1/1/1")
        self.assertEqual(n["remote_chassis_id"], "50:9A:4C:00:00:01")

    def test_missing_system_name_and_mac_port(self):
        n = self.neighbors[1]
        self.assertIsNone(n["remote_system_name"])
        self.assertIsNone(n["remote_interface"])
        self.assertEqual(n["remote_port_id"], "00:1B:21:AA:BB:CC")

    def test_overflowing_column_falls_back_safely(self):
        neighbors, warnings = parse_os6_lldp_remote_device_all(fixture("os6_overflow.txt"))
        self.assertEqual(len(neighbors), 1)
        self.assertEqual(neighbors[0]["remote_port_id"], "TenGigabitEthernet1/0/1")
        self.assertEqual(neighbors[0]["remote_system_name"], "core-x")

    def test_no_neighbors(self):
        neighbors, _ = parse_os6_lldp_remote_device_all(fixture("os6_no_neighbors.txt"))
        self.assertEqual(neighbors, [])

    def test_truncated_output_fails(self):
        with self.assertRaises(LldpParseError):
            parse_os6_lldp_remote_device_all(fixture("os6_truncated.txt"))

    def test_invalid_command_output_fails(self):
        with self.assertRaises(LldpParseError):
            parse_os6_lldp_remote_device_all("% Invalid input detected at '^' marker.")


class DispatchTests(unittest.TestCase):
    def test_platform_dispatch(self):
        self.assertEqual(len(parse_lldp_output("dell_os10", fixture("os10_multiple.txt"))[0]), 5)
        self.assertEqual(len(parse_lldp_output("dell_os6", fixture("os6_multiple.txt"))[0]), 4)
        with self.assertRaises(LldpParseError):
            parse_lldp_output("unknown", "x")


class CorrelationTests(unittest.TestCase):
    def test_managed_unmanaged_and_suffix(self):
        neighbors, _ = parse_os6_lldp_remote_device_all(fixture("os6_multiple.txt"))
        result = correlate(neighbors, INVENTORY + [{"id": 99, "hostname": "unmanaged-sw", "management_ip": "10.9.9.9"}], local_device_id=1)
        self.assertEqual(result[0]["remote_device_id"], 12)       # Kenda-Core-1 by hostname
        self.assertEqual(result[0]["match_method"], "hostname")
        self.assertIsNone(result[1]["remote_device_id"])          # no system name
        self.assertIsNone(result[2]["remote_device_id"])          # IP phone, unmanaged
        self.assertEqual(result[3]["remote_device_id"], 99)       # DNS suffix stripped, unique
        self.assertEqual(result[3]["match_method"], "hostname_short")

    def test_case_insensitive_and_trimmed(self):
        result = correlate([{"remote_system_name": "  kenda-core-2 ", "remote_management_ip": None}], INVENTORY, 1)
        self.assertEqual(result[0]["remote_device_id"], 13)

    def test_management_ip_takes_priority(self):
        n = {"remote_system_name": "Kenda-Core-2", "remote_management_ip": "10.0.0.10"}
        self.assertEqual(correlate([n], INVENTORY, 1)[0]["remote_device_id"], 12)

    def test_ambiguous_hostname_never_matches(self):
        dupes = INVENTORY + [{"id": 50, "hostname": "KENDA-CORE-1", "management_ip": "10.1.1.1"}]
        result = correlate([{"remote_system_name": "Kenda-Core-1", "remote_management_ip": None}], dupes, 1)
        self.assertIsNone(result[0]["remote_device_id"])

    def test_self_reference_not_linked(self):
        result = correlate([{"remote_system_name": "Kenda-HARO-IDF-A", "remote_management_ip": None}], INVENTORY, 1)
        self.assertIsNone(result[0]["remote_device_id"])


class RealOutputTests(unittest.TestCase):
    """
    Exercise real captures once they are added (see fixtures/README.md). Each check is
    format-level so harmless whitespace changes do not break it; exact values live in the
    optional <fixture>.expected.json, written after comparing the CLI by eye.
    """

    INTERFACE = re.compile(r"^[A-Za-z][A-Za-z\-]*\s?\d+(/\d+)*(:\d+)?$")

    def _check(self, platform, name):
        path = os.path.join(FIXTURES, name)
        if not os.path.exists(path):
            self.skipTest(f"{name} not captured yet")

        output = fixture(name)
        neighbors, warnings = parse_lldp_output(platform, output)

        self.assertEqual(warnings, [], f"rows skipped: {warnings}")
        self.assertGreater(len(neighbors), 0, "real capture should contain at least one neighbor")
        self.assertEqual(len(neighbors), count_table_rows(output),
                         "parsed neighbors != data rows in the table (wrapped or missed rows?)")

        for n in neighbors:
            self.assertRegex(n["local_interface"], self.INTERFACE)
            self.assertTrue(n["remote_chassis_id"] or n["remote_port_id"],
                            f"{n['local_interface']}: no remote identity parsed")
            for value in n.values():
                if isinstance(value, str):
                    self.assertEqual(value, value.strip(), "unstripped value")

        expected_path = path.replace(".txt", ".expected.json")
        if os.path.exists(expected_path):
            with open(expected_path) as handle:
                expected = json.load(handle)
            for row in expected:
                self.assertTrue(
                    any(all(n.get(k) == v for k, v in row.items()) for n in neighbors),
                    f"expected neighbor not parsed: {row}",
                )

    def test_real_os10(self):
        self._check("dell_os10", "os10_real_show_lldp_neighbors.txt")

    def test_real_os6(self):
        self._check("dell_os6", "os6_real_show_lldp_remote_device_all.txt")


class RowCounterTests(unittest.TestCase):
    def test_counts_match_synthetic_parses(self):
        for platform, name in (("dell_os10", "os10_multiple.txt"), ("dell_os6", "os6_multiple.txt")):
            out = fixture(name)
            self.assertEqual(count_table_rows(out), len(parse_lldp_output(platform, out)[0]))
        self.assertIsNone(count_table_rows(fixture("os10_truncated.txt")))


if __name__ == "__main__":
    unittest.main()
