"""Explicit LLDP identity mapping + correlation order. Pure (no DB)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from topology.correlate import correlate  # noqa: E402
from topology.identity import normalize_chassis_id, normalize_system_name  # noqa: E402

INVENTORY = [
    {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "10.0.0.10"},
    {"id": 13, "hostname": "Kenda-Core-2", "management_ip": "10.0.0.20"},
    {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "10.0.0.31"},
    {"id": 4, "hostname": "Kenda-HARO-SW-01", "management_ip": "10.0.0.4"},
    {"id": 20, "hostname": "Kenda-HQ-IDF-A", "management_ip": "10.0.0.29"},
]


def neighbor(name=None, chassis=None, ip=None, local="ethernet1/1/25", port="ethernet1/1/25"):
    return {"local_interface": local, "remote_system_name": name, "remote_chassis_id": chassis,
            "remote_management_ip": ip, "remote_port_id": port, "remote_interface": port}


def one(n, identities=None, local=12):
    return correlate([n], INVENTORY, local, identities)[0]


class NormalizationTests(unittest.TestCase):
    def test_chassis_forms_compare_equal(self):
        forms = ["E8:B5:D0:7A:5C:A3", "e8:b5:d0:7a:5c:a3", "e8-b5-d0-7a-5c-a3", "e8b5.d07a.5ca3", "E8B5D07A5CA3", "  e8:b5:d0:7a:5c:a3 "]
        self.assertEqual({normalize_chassis_id(f) for f in forms}, {"e8:b5:d0:7a:5c:a3"})

    def test_non_mac_chassis_not_rewritten(self):
        self.assertEqual(normalize_chassis_id(" Switch-Chassis-7 "), "switch-chassis-7")
        self.assertEqual(normalize_chassis_id("e8:b5:d0"), "e8:b5:d0")  # not a MAC: left as-is
        self.assertIsNone(normalize_chassis_id("   "))

    def test_system_name_only_trimmed_and_casefolded(self):
        self.assertEqual(normalize_system_name("  KENDA-HQ-1 "), "kenda-hq-1")
        # No zero stripping, no punctuation rewriting.
        self.assertNotEqual(normalize_system_name("kenda-core-02"), normalize_system_name("Kenda-Core-2"))
        self.assertNotEqual(normalize_system_name("HARO_SW_01"), normalize_system_name("Kenda-HARO-SW-01"))


class IdentityCorrelationTests(unittest.TestCase):
    def test_exact_system_name_identity(self):
        r = one(neighbor("kenda-core-02"), [{"device_id": 13, "lldp_system_name": "kenda-core-02", "chassis_id": None}])
        self.assertEqual((r["remote_device_id"], r["match_type"]), (13, "lldp_system_name"))

    def test_identity_name_is_case_insensitive(self):
        r = one(neighbor("KENDA-CORE-02"), [{"device_id": 13, "lldp_system_name": "kenda-core-02", "chassis_id": None}])
        self.assertEqual(r["remote_device_id"], 13)

    def test_chassis_identity_with_normalization(self):
        r = one(neighbor("anything", chassis="E8-B5-D0-7A-5C-A3"),
                [{"device_id": 13, "lldp_system_name": None, "chassis_id": "e8:b5:d0:7a:5c:a3"}])
        self.assertEqual((r["remote_device_id"], r["match_type"]), (13, "lldp_chassis"))

    def test_chassis_takes_priority_over_hostname(self):
        # Advertised name equals a real hostname, but the explicit chassis mapping wins.
        r = one(neighbor("Kenda-HQ-IDF-A", chassis="aa:aa:aa:aa:aa:aa"),
                [{"device_id": 4, "lldp_system_name": None, "chassis_id": "aa:aa:aa:aa:aa:aa"}])
        self.assertEqual((r["remote_device_id"], r["match_type"]), (4, "lldp_chassis"))

    def test_name_and_chassis_agree(self):
        ids = [{"device_id": 13, "lldp_system_name": "kenda-core-02", "chassis_id": None},
               {"device_id": 13, "lldp_system_name": None, "chassis_id": "e8:b5:d0:7a:5c:a3"}]
        r = one(neighbor("kenda-core-02", chassis="e8:b5:d0:7a:5c:a3"), ids)
        self.assertEqual((r["remote_device_id"], r["match_type"]), (13, "lldp_chassis"))
        self.assertIsNone(r["correlation_note"])

    def test_name_and_chassis_conflict_never_chooses(self):
        ids = [{"device_id": 13, "lldp_system_name": "kenda-core-02", "chassis_id": None},
               {"device_id": 20, "lldp_system_name": None, "chassis_id": "e8:b5:d0:7a:5c:a3"}]
        r = one(neighbor("kenda-core-02", chassis="e8:b5:d0:7a:5c:a3"), ids)
        self.assertIsNone(r["remote_device_id"])
        self.assertEqual(r["match_type"], "conflict")
        self.assertIn("conflict", r["correlation_note"])

    def test_conflict_does_not_fall_through_to_hostname(self):
        # Even though "Kenda-Core-2" is an exact hostname, a conflicting identity blocks the match.
        ids = [{"device_id": 13, "lldp_system_name": "Kenda-Core-2", "chassis_id": None},
               {"device_id": 20, "lldp_system_name": None, "chassis_id": "e8:b5:d0:7a:5c:a3"}]
        r = one(neighbor("Kenda-Core-2", chassis="e8:b5:d0:7a:5c:a3"), ids)
        self.assertIsNone(r["remote_device_id"])
        self.assertEqual(r["match_type"], "conflict")

    def test_device_with_multiple_aliases(self):
        ids = [{"device_id": 4, "lldp_system_name": "HARO_SW_01", "chassis_id": None},
               {"device_id": 4, "lldp_system_name": "haro-sw-01-old", "chassis_id": None},
               {"device_id": 4, "lldp_system_name": None, "chassis_id": "f0:d4:e2:95:6b:1d"}]
        for n in (neighbor("HARO_SW_01"), neighbor("haro-sw-01-old"), neighbor(None, chassis="F0:D4:E2:95:6B:1D")):
            self.assertEqual(one(n, ids, local=1)["remote_device_id"], 4)

    def test_identity_to_self_is_blocked(self):
        r = one(neighbor("kenda-core-01"), [{"device_id": 12, "lldp_system_name": "kenda-core-01", "chassis_id": None}], local=12)
        self.assertIsNone(r["remote_device_id"])
        self.assertEqual(r["match_type"], "self")


class ExistingBehaviourTests(unittest.TestCase):
    def test_management_ip_still_matches(self):
        r = one(neighbor("unknown-name", ip="10.0.0.20"))
        self.assertEqual((r["remote_device_id"], r["match_type"]), (13, "management_ip"))

    def test_exact_hostname_still_matches(self):
        r = one(neighbor("kenda-core-2"))
        self.assertEqual((r["remote_device_id"], r["match_type"], r["match_method"]), (13, "hostname", "hostname"))

    def test_ambiguous_hostname_still_unmatched(self):
        dupes = INVENTORY + [{"id": 50, "hostname": "KENDA-CORE-2", "management_ip": "10.9.9.9"}]
        r = correlate([neighbor("Kenda-Core-2")], dupes, 12)[0]
        self.assertIsNone(r["remote_device_id"])

    def test_no_fuzzy_match_without_identity(self):
        for name in ("kenda-core-02", "HARO_SW_01", "HQ-KENDA-2", "KENDA-HQ-1"):
            r = one(neighbor(name))
            self.assertIsNone(r["remote_device_id"], name)
            self.assertEqual(r["match_type"], "unmanaged")

    def test_unmatched_endpoint_stays_unmanaged(self):
        r = one(neighbor("Broadcom Adv. Dua...", chassis="04:32:01:c5:9f:c1", port="04:32:01:c5:9f:c1"),
                [{"device_id": 13, "lldp_system_name": "kenda-core-02", "chassis_id": None}])
        self.assertEqual((r["remote_device_id"], r["match_type"]), (None, "unmanaged"))


if __name__ == "__main__":
    unittest.main()


class RealTopologyIdentityTests(unittest.TestCase):
    """
    All four live captures + the 4 CONFIRMED identities (db/seeds/lldp_identity_confirmed.sql).
    Locks in: exactly the reciprocally-proven links become managed; nothing else does.
    """

    FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    INVENTORY = [
        {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "10.0.0.10"},
        {"id": 13, "hostname": "Kenda-Core-2", "management_ip": "10.0.0.20"},
        {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "10.0.0.31"},
        {"id": 19, "hostname": "Kenda-HARO-SW-01", "management_ip": "10.0.0.4"},
        {"id": 20, "hostname": "Kenda-HARO-SW-02", "management_ip": "10.0.0.25"},
        {"id": 16, "hostname": "Kenda-HQ-IDF-A", "management_ip": "10.0.0.29"},
        {"id": 17, "hostname": "Kenda-HQ-IDF-B", "management_ip": "10.0.0.30"},
        {"id": 18, "hostname": "Kenda-HQ-SW-01", "management_ip": "10.0.0.3"},
        {"id": 14, "hostname": "Kenda-Access-user-1", "management_ip": "10.0.0.250"},
        {"id": 15, "hostname": "Kenda-Access-user-2", "management_ip": "10.0.0.251"},
    ]
    CONFIRMED = [
        {"device_id": 12, "lldp_system_name": "kenda-core-01", "chassis_id": "e8:b5:d0:7a:61:a3"},
        {"device_id": 13, "lldp_system_name": "kenda-core-02", "chassis_id": "e8:b5:d0:7a:5c:a3"},
        {"device_id": 19, "lldp_system_name": "HARO_SW_01", "chassis_id": "f0:d4:e2:95:6b:1d"},
        {"device_id": 1, "lldp_system_name": "HARO-IDF-A", "chassis_id": "e8:b2:65:4c:c2:68"},
    ]
    CAPTURES = [
        (12, "dell_os10", "os10_real_show_lldp_neighbors.txt", 22),
        (13, "dell_os10", "os10_real_Kenda-Core-2.txt", 15),
        (1, "dell_os6", "os6_real_show_lldp_remote_device_all.txt", 11),
        (19, "dell_os6", "os6_real_Kenda-HARO-SW-01.txt", 9),
    ]

    @classmethod
    def setUpClass(cls):
        from topology.lldp import count_table_rows, parse_lldp_output

        cls.rows = {}
        for device_id, platform, name, expected in cls.CAPTURES:
            path = os.path.join(cls.FIXTURES, name)
            if not os.path.exists(path):
                raise unittest.SkipTest(f"{name} missing")
            output = open(path).read()
            neighbors, warnings = parse_lldp_output(platform, output)
            assert warnings == [], (name, warnings)
            assert len(neighbors) == expected == count_table_rows(output), (name, len(neighbors))
            cls.rows[device_id] = correlate(neighbors, cls.INVENTORY, device_id, cls.CONFIRMED)

    def managed(self):
        return sorted((src, n["local_interface"], n["remote_device_id"], n["match_type"])
                      for src, rows in self.rows.items() for n in rows if n["remote_device_id"])

    def test_only_reciprocally_proven_links_are_managed(self):
        self.assertEqual(self.managed(), [
            (1, "Tw1/0/4", 19, "lldp_chassis"),
            (12, "ethernet1/1/25", 13, "lldp_chassis"),
            (13, "ethernet1/1/25", 12, "lldp_chassis"),
            (19, "Te1/0/3", 1, "lldp_chassis"),
        ])

    def test_no_conflicts_or_self_links(self):
        types = {n["match_type"] for rows in self.rows.values() for n in rows}
        self.assertNotIn("conflict", types)
        self.assertNotIn("self", types)

    def test_unresolved_identities_stay_unmanaged(self):
        for rows in self.rows.values():
            for n in rows:
                if n["remote_system_name"] in ("HQ-KENDA-2", "KENDA-HQ-1", "Kenda-HQ", "HARO_SW_02", "Haro-IDF-B",
                                               "VN26KYG0X3", "Kenda-HQ-Array01-A", "Kenda-HQ-Array01-B",
                                               "Broadcom Adv. Dua..."):
                    self.assertIsNone(n["remote_device_id"], n["remote_system_name"])
                    self.assertEqual(n["match_type"], "unmanaged")

    def test_new_real_captures_parse_exact_rows(self):
        core2 = {n["local_interface"]: n for n in self.rows[13]}
        self.assertEqual(core2["ethernet1/1/25"]["remote_system_name"], "kenda-core-01")
        self.assertEqual(core2["ethernet1/1/26:4"]["remote_port_id"], "Te1/0/3")
        self.assertEqual(core2["ethernet1/1/49"]["remote_system_name"], "Broadcom Adv. Dua...")
        sw01 = {n["local_interface"]: n for n in self.rows[19]}
        self.assertEqual((sw01["Te1/0/3"]["remote_system_name"], sw01["Te1/0/3"]["remote_port_id"]), ("HARO-IDF-A", "Tw1/0/4"))
        self.assertEqual((sw01["Gi1/0/1"]["remote_system_name"], sw01["Gi1/0/1"]["remote_port_id"]), ("HARO_SW_02", "Gi1/0/1"))
        # Chassis ID advertised as a hostname-like string, MAC Port ID, no system name.
        self.assertEqual((sw01["Gi1/0/6"]["remote_chassis_id"], sw01["Gi1/0/6"]["remote_port_id"]), ("KEN-D1RNQPX2", "E4:54:E8:51:5E:66"))
        self.assertIsNone(sw01["Gi1/0/6"]["remote_system_name"])
        self.assertIsNone(sw01["Gi1/0/6"]["remote_interface"])
