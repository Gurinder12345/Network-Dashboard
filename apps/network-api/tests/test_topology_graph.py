"""Graph construction / physical-link dedup tests. Run from apps/network-api:
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.topology_graph import build_topology  # noqa: E402

T1, T2 = "2026-09-30T10:00:00+00:00", "2026-09-30T10:05:00+00:00"

DEVICES = [
    {"id": 12, "hostname": "Kenda-Core-1", "management_ip": "10.0.0.10", "platform": "dell_os10", "enabled": True},
    {"id": 13, "hostname": "Kenda-Core-2", "management_ip": "10.0.0.20", "platform": "dell_os10", "enabled": True},
    {"id": 1, "hostname": "Kenda-HARO-IDF-A", "management_ip": "10.0.0.31", "platform": "dell_os6", "enabled": True},
    {"id": 7, "hostname": "lab-disabled", "management_ip": "10.0.0.99", "platform": "dell_os6", "enabled": False},
]


def obs(local, local_if, remote=None, remote_if=None, name=None, chassis=None, port=None, seen=T2, active=True):
    return {
        "local_device_id": local, "local_interface": local_if, "remote_device_id": remote,
        "remote_system_name": name, "remote_interface": remote_if, "remote_management_ip": None,
        "remote_chassis_id": chassis, "remote_port_id": port or remote_if, "protocol": "lldp",
        "first_seen_at": T1, "last_seen_at": seen, "active": active,
    }


def managed_edges(graph):
    return [e for e in graph["links"] if e["relationship"] == "managed"]


class DedupTests(unittest.TestCase):
    def test_bidirectional_becomes_one_edge(self):
        g = build_topology(DEVICES, [
            obs(12, "ethernet1/1/1", 1, "Te1/0/48"),
            obs(1, "Te1/0/48", 12, "ethernet1/1/1"),
        ], {})
        edges = managed_edges(g)
        self.assertEqual(len(edges), 1)
        self.assertTrue(edges[0]["observed_bidirectionally"])
        self.assertEqual(edges[0]["observations"], 2)
        self.assertEqual({edges[0]["source"], edges[0]["target"]}, {"device:12", "device:1"})

    def test_interface_names_compared_case_and_space_insensitively(self):
        g = build_topology(DEVICES, [
            obs(12, "ethernet1/1/1", 1, "te1/0/48"),
            obs(1, "Te1/0/48", 12, "Ethernet 1/1/1"),
        ], {})
        self.assertEqual(len(managed_edges(g)), 1)
        self.assertTrue(managed_edges(g)[0]["observed_bidirectionally"])

    def test_one_sided_link_still_rendered(self):
        g = build_topology(DEVICES, [obs(12, "ethernet1/1/2", 1, "Te1/0/47")], {})
        edges = managed_edges(g)
        self.assertEqual(len(edges), 1)
        self.assertFalse(edges[0]["observed_bidirectionally"])

    def test_parallel_links_same_devices_not_merged(self):
        g = build_topology(DEVICES, [
            obs(12, "ethernet1/1/10", 13, "ethernet1/1/10"),
            obs(13, "ethernet1/1/10", 12, "ethernet1/1/10"),
            obs(12, "ethernet1/1/11", 13, "ethernet1/1/11"),
            obs(13, "ethernet1/1/11", 12, "ethernet1/1/11"),
        ], {})
        edges = managed_edges(g)
        self.assertEqual(len(edges), 2)
        self.assertTrue(all(e["observed_bidirectionally"] for e in edges))

    def test_partial_pairing_when_one_side_lacks_interface(self):
        # Core saw a MAC Port ID (remote_interface None); IDF saw the core's interface name.
        g = build_topology(DEVICES, [
            obs(12, "ethernet1/1/3", 1, None, port="f8:b1:56:aa:bb:01"),
            obs(1, "Te1/0/46", 12, "ethernet1/1/3"),
        ], {})
        edges = managed_edges(g)
        self.assertEqual(len(edges), 1)
        self.assertTrue(edges[0]["observed_bidirectionally"])

    def test_partial_pairing_refuses_ambiguity(self):
        # Two IDF ports both claim core ethernet1/1/3 while core's side has no interface:
        # cannot decide -> all rendered one-sided, nothing merged.
        g = build_topology(DEVICES, [
            obs(12, "ethernet1/1/3", 1, None, port="mac-x"),
            obs(1, "Te1/0/46", 12, "ethernet1/1/3"),
            obs(1, "Te1/0/45", 12, "ethernet1/1/3", port="ethernet1/1/3 "),
        ], {})
        edges = managed_edges(g)
        self.assertEqual(len(edges), 3)
        self.assertFalse(any(e["observed_bidirectionally"] for e in edges))

    def test_edge_ids_stable_regardless_of_direction(self):
        a = build_topology(DEVICES, [obs(12, "ethernet1/1/1", 1, "Te1/0/48"), obs(1, "Te1/0/48", 12, "ethernet1/1/1")], {})
        b = build_topology(DEVICES, [obs(1, "Te1/0/48", 12, "ethernet1/1/1"), obs(12, "ethernet1/1/1", 1, "Te1/0/48")], {})
        self.assertEqual(managed_edges(a)[0]["id"], managed_edges(b)[0]["id"])


class NodeTests(unittest.TestCase):
    def test_unmanaged_neighbors_grouped_by_chassis(self):
        g = build_topology(DEVICES, [
            obs(12, "ethernet1/1/20", None, None, name="unmanaged-ap-01", chassis="C0:FF:EE:00:00:20", port="c0:ff:ee:00:00:20"),
            obs(1, "Gi1/0/20", None, "Gi0/1", name="unmanaged-ap-01", chassis="c0:ff:ee:00:00:20"),
        ], {})
        externals = [n for n in g["nodes"] if not n["managed"]]
        self.assertEqual(len(externals), 1)
        self.assertEqual(sorted(externals[0]["attached_device_ids"]), [1, 12])
        self.assertEqual(g["unmanaged_neighbors"], 1)
        self.assertEqual(len([e for e in g["links"] if e["relationship"] == "unmanaged"]), 2)

    def test_neighbor_without_identity_stays_separate(self):
        g = build_topology(DEVICES, [
            obs(1, "Gi1/0/5", None, None, port="aa"),
            obs(1, "Gi1/0/6", None, None, port="bb"),
        ], {})
        self.assertEqual(len([n for n in g["nodes"] if not n["managed"]]), 2)

    def test_disabled_device_hidden_unless_linked(self):
        g = build_topology(DEVICES, [], {})
        self.assertNotIn("device:7", {n["id"] for n in g["nodes"]})
        g = build_topology(DEVICES, [obs(1, "Gi1/0/9", 7, "Gi1/0/1")], {})
        self.assertIn("device:7", {n["id"] for n in g["nodes"]})

    def test_counts_and_discovery_timestamps(self):
        states = {12: {"last_success_at": T2, "last_attempt_at": T2, "last_error": None},
                  1: {"last_success_at": T1, "last_attempt_at": T2, "last_error": "LldpParseError: x"}}
        g = build_topology(DEVICES, [obs(12, "ethernet1/1/1", 1, "Te1/0/48")], states)
        self.assertEqual(g["managed_devices"], 3)
        self.assertEqual(g["active_links"], 1)
        self.assertEqual(g["last_discovery_at"], T2)
        self.assertEqual(g["failing_devices"], 1)

    def test_no_links_no_fabrication(self):
        g = build_topology(DEVICES, [], {})
        self.assertEqual(g["links"], [])
        self.assertEqual(g["unmanaged_neighbors"], 0)


if __name__ == "__main__":
    unittest.main()
