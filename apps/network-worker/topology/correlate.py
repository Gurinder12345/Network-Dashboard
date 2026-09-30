"""
Map LLDP neighbors onto the managed inventory. Pure function; never creates devices.

Match priority (first unambiguous hit wins):
  1. Explicit operator-maintained LLDP identity (device_lldp_identity):
       a. chassis ID  (normalized, see topology.identity)
       b. system name (trimmed, case-insensitive)
     If the chassis maps to one device and the name to another, that is a CONFLICT:
     nothing is chosen, the neighbor stays unmanaged and a warning is reported.
  2. remote_management_ip == devices.management_ip
  3. remote_system_name == devices.hostname (trimmed, case-insensitive)
  4. same, after stripping a DNS suffix -- only when the short name maps to exactly one
     device and the full name matched none
  5. otherwise unmanaged
No fuzzy matching: no zero stripping, punctuation removal or similarity scoring.
Ambiguous names (two devices with the same normalized hostname) never match.
"""

from topology.identity import build_identity_index, normalize_chassis_id, normalize_system_name

# match_type values
LLDP_CHASSIS = "lldp_chassis"
LLDP_SYSTEM_NAME = "lldp_system_name"
MANAGEMENT_IP = "management_ip"
HOSTNAME = "hostname"
SHORT_HOSTNAME = "short_hostname"
UNMANAGED = "unmanaged"
CONFLICT = "conflict"
SELF = "self"

# Legacy match_method names kept for existing callers/tests.
_LEGACY_METHOD = {MANAGEMENT_IP: "management_ip", HOSTNAME: "hostname", SHORT_HOSTNAME: "hostname_short",
                  LLDP_CHASSIS: "lldp_chassis", LLDP_SYSTEM_NAME: "lldp_system_name"}


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
    candidates = list(candidates)
    return candidates[0] if len(candidates) == 1 else None


def _match_identity(neighbor, identity_index):
    """Returns (device_id, match_type, note) from explicit identities, or (None, None, None)."""
    by_chassis, by_name = identity_index
    chassis = normalize_chassis_id(neighbor.get("remote_chassis_id"))
    name = normalize_system_name(neighbor.get("remote_system_name"))

    chassis_ids = by_chassis.get(chassis, set()) if chassis else set()
    name_ids = by_name.get(name, set()) if name else set()

    if len(chassis_ids) > 1 or len(name_ids) > 1:
        return None, CONFLICT, "LLDP identity maps to more than one device"

    if chassis_ids and name_ids and chassis_ids != name_ids:
        return None, CONFLICT, (
            f"LLDP identity conflict: chassis {chassis} -> device {next(iter(chassis_ids))}, "
            f"system name {neighbor.get('remote_system_name')!r} -> device {next(iter(name_ids))}"
        )

    if chassis_ids:
        return next(iter(chassis_ids)), LLDP_CHASSIS, None
    if name_ids:
        return next(iter(name_ids)), LLDP_SYSTEM_NAME, None
    return None, None, None


def match_device(neighbor, index, identity_index=None):
    """
    Returns (device_id or None, legacy method or None).
    Use classify_neighbor() for the full match_type / conflict information.
    """
    device_id, match_type, _ = classify_neighbor(neighbor, index, identity_index)
    return device_id, _LEGACY_METHOD.get(match_type)


def classify_neighbor(neighbor, index, identity_index=None):
    """Returns (device_id or None, match_type, note or None)."""
    if identity_index:
        device_id, match_type, note = _match_identity(neighbor, identity_index)
        if match_type == CONFLICT:
            return None, CONFLICT, note
        if device_id is not None:
            return device_id, match_type, None

    by_ip, by_name = index

    ip = _norm(neighbor.get("remote_management_ip"))
    if ip:
        device_id = _unique(by_ip.get(ip, []))
        if device_id is not None:
            return device_id, MANAGEMENT_IP, None

    name = _norm(neighbor.get("remote_system_name"))
    if name:
        device_id = _unique(by_name.get(name, []))
        if device_id is not None:
            return device_id, HOSTNAME, None

        if name not in by_name and "." in name:
            device_id = _unique(by_name.get(name.split(".", 1)[0], []))
            if device_id is not None:
                return device_id, SHORT_HOSTNAME, None

    return None, UNMANAGED, None


def correlate(neighbors, devices, local_device_id, identities=None):
    """
    Adds remote_device_id, match_type (and legacy match_method, correlation_note) to each
    neighbor. Copies; input untouched. identities: rows from device_lldp_identity.
    """
    index = build_inventory_index(devices)
    identity_index = build_identity_index(identities) if identities else None
    result = []

    for neighbor in neighbors:
        device_id, match_type, note = classify_neighbor(neighbor, index, identity_index)
        if device_id is not None and device_id == local_device_id:
            # A device reporting itself (loop / misreport): keep the row, don't link to self.
            device_id, match_type, note = None, SELF, "neighbor resolves to the reporting device itself"
        result.append({
            **neighbor,
            "remote_device_id": device_id,
            "match_type": match_type,
            "match_method": _LEGACY_METHOD.get(match_type),
            "correlation_note": note,
        })

    return result
