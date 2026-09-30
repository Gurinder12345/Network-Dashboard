"""
Map LLDP neighbors onto the managed inventory. Pure function; never creates devices.

Match priority (first unambiguous hit wins):
  1. remote_management_ip == devices.management_ip
  2. remote_system_name == devices.hostname (trimmed, case-insensitive)
  3. same, after stripping a DNS suffix -- only when the short name maps to exactly one
     device and the full name matched none
Chassis ID is not used: the inventory holds no chassis-to-device mapping, so a match on
it would be a guess. Ambiguous names (two devices with the same normalized hostname)
never match. Unmatched neighbors keep remote_device_id = None and remain external nodes.
"""


def _norm(value):
    return value.strip().lower() if value else ""


def build_inventory_index(devices):
    by_ip = {}
    by_name = {}

    for device in devices:
        ip = _norm(str(device.get("management_ip") or ""))
        name = _norm(device.get("hostname"))
        if ip:
            by_ip.setdefault(ip, []).append(device["id"])
        if name:
            by_name.setdefault(name, []).append(device["id"])

    return by_ip, by_name


def _unique(candidates):
    return candidates[0] if candidates and len(candidates) == 1 else None


def match_device(neighbor, index):
    """Returns (device_id or None, method or None)."""
    by_ip, by_name = index

    ip = _norm(neighbor.get("remote_management_ip"))
    if ip:
        device_id = _unique(by_ip.get(ip, []))
        if device_id is not None:
            return device_id, "management_ip"

    name = _norm(neighbor.get("remote_system_name"))
    if name:
        device_id = _unique(by_name.get(name, []))
        if device_id is not None:
            return device_id, "hostname"

        if name not in by_name and "." in name:
            device_id = _unique(by_name.get(name.split(".", 1)[0], []))
            if device_id is not None:
                return device_id, "hostname_short"

    return None, None


def correlate(neighbors, devices, local_device_id):
    """Adds remote_device_id / match_method to each neighbor (copies; input untouched)."""
    index = build_inventory_index(devices)
    result = []

    for neighbor in neighbors:
        device_id, method = match_device(neighbor, index)
        if device_id == local_device_id:
            # A device reporting itself (loop / misreport): keep the row, don't link to self.
            device_id, method = None, None
        result.append({**neighbor, "remote_device_id": device_id, "match_method": method})

    return result
