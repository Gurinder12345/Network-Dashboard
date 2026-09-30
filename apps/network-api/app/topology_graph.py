"""
Build the topology graph from directional LLDP observations. Pure functions (no DB/Redis)
so node/edge construction and physical-link deduplication are unit-testable.

Directional rows ("A:ifA saw B:ifB") are folded into physical edges:
  1. Exact pairing: A:ifA->B:ifB and B:ifB->A:ifA share one unordered endpoint key.
  2. Partial pairing: when one side's advertised Port ID was not an interface name
     (remote_interface NULL), it pairs with the reverse observation only if exactly one
     compatible candidate exists and at least one interface is confirmed by both sides.
  Anything unpaired is still an edge, marked observed_bidirectionally = false.
  Links between the same two devices on different interfaces are never merged.
Neighbors not matched to the inventory become external (unmanaged) nodes; they are never
merged into managed devices and never get a health state.
"""

import hashlib
import re


def _norm_if(value):
    return re.sub(r"\s+", "", value).lower() if value else None


def _digest(text, length=16):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _later(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def external_key(link):
    """Stable identity for an unmanaged neighbor: chassis ID, else advertised name."""
    chassis = (link.get("remote_chassis_id") or "").strip().lower()
    if chassis:
        return f"chassis:{chassis}"
    name = (link.get("remote_system_name") or "").strip().lower()
    if name:
        return f"name:{name}"
    # No identity advertised: unique to where it was seen.
    return f"port:{link['local_device_id']}:{_norm_if(link['local_interface'])}:{(link.get('remote_port_id') or '').lower()}"


def _managed_edge(first, second, bidirectional):
    """first is A->B (always present); second is the B->A observation or None."""
    a_if = first["local_interface"]
    b_if = second["local_interface"] if second else first.get("remote_interface")

    ends = sorted(
        [(f"device:{first['local_device_id']}", a_if), (f"device:{first['remote_device_id']}", b_if)],
        key=lambda end: (end[0], _norm_if(end[1]) or ""),
    )
    key = "|".join(f"{node}:{_norm_if(iface) or '?'}" for node, iface in ends)
    rows = [first] + ([second] if second else [])

    return {
        "id": f"link:{_digest(key)}",
        "source": ends[0][0],
        "target": ends[1][0],
        "source_interface": ends[0][1],
        "target_interface": ends[1][1],
        "protocol": first.get("protocol", "lldp"),
        "first_seen_at": min(r["first_seen_at"] for r in rows),
        "last_seen_at": max(r["last_seen_at"] for r in rows),
        "active": any(r["active"] for r in rows),
        "observed_bidirectionally": bidirectional,
        "relationship": "managed",
        "observations": len(rows),
    }


def _pair_managed(observations):
    edges = []
    exact = {}
    pending = []

    for obs in observations:
        if obs.get("remote_interface"):
            key = frozenset({
                (obs["local_device_id"], _norm_if(obs["local_interface"])),
                (obs["remote_device_id"], _norm_if(obs["remote_interface"])),
            })
            exact.setdefault(key, []).append(obs)
        else:
            pending.append(obs)

    for group in exact.values():
        forward = group[0]
        reverse = next((o for o in group[1:] if o["local_device_id"] != forward["local_device_id"]), None)
        if reverse:
            edges.append(_managed_edge(forward, reverse, True))
        else:
            pending.append(forward)  # same-direction duplicates collapse into one

    used = set()
    for index, obs in enumerate(pending):
        if index in used:
            continue

        candidates = []
        for other_index, other in enumerate(pending):
            if other_index == index or other_index in used:
                continue
            if other["local_device_id"] != obs["remote_device_id"] or other["remote_device_id"] != obs["local_device_id"]:
                continue
            fwd_ok = obs.get("remote_interface") is None or _norm_if(obs["remote_interface"]) == _norm_if(other["local_interface"])
            rev_ok = other.get("remote_interface") is None or _norm_if(other["remote_interface"]) == _norm_if(obs["local_interface"])
            confirmed = obs.get("remote_interface") is not None or other.get("remote_interface") is not None
            if fwd_ok and rev_ok and confirmed:
                candidates.append(other_index)

        if len(candidates) == 1:
            used.update({index, candidates[0]})
            edges.append(_managed_edge(obs, pending[candidates[0]], True))
        else:
            used.add(index)
            edges.append(_managed_edge(obs, None, False))

    return edges


def build_topology(devices, links, states):
    """
    devices: inventory rows (id, hostname, management_ip, platform, enabled)
    links:   topology_links rows (dicts, timestamps as ISO strings)
    states:  {device_id: {last_attempt_at, last_success_at, last_error, neighbors_seen}}
    Returns a JSON-ready structure WITHOUT health (health is overlaid per request).
    """
    by_id = {d["id"]: d for d in devices}
    referenced = {l["local_device_id"] for l in links} | {l["remote_device_id"] for l in links if l.get("remote_device_id")}

    nodes = {}
    for device in devices:
        if not device["enabled"] and device["id"] not in referenced:
            continue
        state = states.get(device["id"], {})
        nodes[f"device:{device['id']}"] = {
            "id": f"device:{device['id']}",
            "device_id": device["id"],
            "hostname": device["hostname"],
            "management_ip": device["management_ip"],
            "platform": device["platform"],
            "managed": True,
            "enabled": device["enabled"],
            "topology_last_seen_at": None,
            "topology_last_attempt_at": state.get("last_attempt_at"),
            "topology_last_success_at": state.get("last_success_at"),
            "topology_last_error": state.get("last_error"),
            "neighbor_count": 0,
        }

    managed_obs = []
    edges = []

    for link in links:
        source = f"device:{link['local_device_id']}"
        if source not in nodes:
            continue
        nodes[source]["topology_last_seen_at"] = _later(nodes[source]["topology_last_seen_at"], link["last_seen_at"])

        if link.get("remote_device_id") and f"device:{link['remote_device_id']}" in nodes:
            managed_obs.append(link)
            continue

        key = external_key(link)
        node_id = f"external:{_digest(key, 12)}"
        node = nodes.setdefault(node_id, {
            "id": node_id,
            "device_id": None,
            "hostname": link.get("remote_system_name") or link.get("remote_chassis_id") or link.get("remote_port_id") or "unidentified neighbor",
            "advertised_system_name": link.get("remote_system_name"),
            "management_ip": link.get("remote_management_ip"),
            "chassis_id": link.get("remote_chassis_id"),
            "platform": None,
            "managed": False,
            "remote_port_ids": [],
            "attached_device_ids": [],
            "topology_last_seen_at": None,
            "neighbor_count": 0,
        })
        if link.get("remote_port_id") and link["remote_port_id"] not in node["remote_port_ids"]:
            node["remote_port_ids"].append(link["remote_port_id"])
        if link["local_device_id"] not in node["attached_device_ids"]:
            node["attached_device_ids"].append(link["local_device_id"])
        node["topology_last_seen_at"] = _later(node["topology_last_seen_at"], link["last_seen_at"])

        edges.append({
            "id": f"link:{_digest(key + '|' + source + ':' + (_norm_if(link['local_interface']) or ''))}",
            "source": source,
            "target": node_id,
            "source_interface": link["local_interface"],
            "target_interface": link.get("remote_interface"),
            "target_port_id": link.get("remote_port_id"),
            "protocol": link.get("protocol", "lldp"),
            "first_seen_at": link["first_seen_at"],
            "last_seen_at": link["last_seen_at"],
            "active": link["active"],
            "observed_bidirectionally": False,
            "relationship": "unmanaged",
            "observations": 1,
        })

    edges.extend(_pair_managed(managed_obs))

    for edge in edges:
        nodes[edge["source"]]["neighbor_count"] += 1
        nodes[edge["target"]]["neighbor_count"] += 1

    successes = [s.get("last_success_at") for s in states.values() if s.get("last_success_at")]
    attempts = [s.get("last_attempt_at") for s in states.values() if s.get("last_attempt_at")]
    node_list = sorted(nodes.values(), key=lambda n: (not n["managed"], (n["hostname"] or "").lower()))

    return {
        "last_discovery_at": max(successes) if successes else None,
        "last_attempt_at": max(attempts) if attempts else None,
        "managed_devices": sum(1 for n in node_list if n["managed"]),
        "active_links": sum(1 for e in edges if e["active"]),
        "unmanaged_neighbors": sum(1 for n in node_list if not n["managed"]),
        "failing_devices": sum(1 for s in states.values() if s.get("last_error")),
        "nodes": node_list,
        "links": sorted(edges, key=lambda e: e["id"]),
    }
