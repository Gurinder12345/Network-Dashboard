"""
Deterministic LLDP identity normalization. Pure functions, no DB access.

System name: trimmed, compared case-insensitively. Nothing else changes -- no stripping
of zeros, punctuation or prefixes, and no fuzzy comparison.

Chassis ID: trimmed and lowercased. A MAC address in any common notation
(aa:bb:.., aa-bb-.., aabb.ccdd.eeff, aabbccddeeff) becomes canonical aa:bb:cc:dd:ee:ff.
Anything that is not a MAC (other LLDP chassis subtypes) is compared as the trimmed
lowercase string, unchanged otherwise.
"""

import re

_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_MAC_FORMS = (
    re.compile(r"^([0-9a-f]{2}[:\-]){5}[0-9a-f]{2}$"),
    re.compile(r"^([0-9a-f]{4}\.){2}[0-9a-f]{4}$"),
    _HEX12,
)


def normalize_system_name(value):
    if value is None:
        return None
    value = value.strip()
    return value.lower() or None


def normalize_chassis_id(value):
    if value is None:
        return None
    value = value.strip().lower()
    if not value:
        return None
    if any(form.match(value) for form in _MAC_FORMS):
        digits = re.sub(r"[^0-9a-f]", "", value)
        return ":".join(digits[i:i + 2] for i in range(0, 12, 2))
    return value


def build_identity_index(identities):
    """
    identities: rows with device_id, lldp_system_name, chassis_id.
    Returns (by_chassis, by_name) mapping normalized value -> set(device_id).
    The DB guarantees uniqueness; sets make any violation visible instead of hidden.
    """
    by_chassis = {}
    by_name = {}

    for row in identities or []:
        chassis = normalize_chassis_id(row.get("chassis_id"))
        name = normalize_system_name(row.get("lldp_system_name"))
        if chassis:
            by_chassis.setdefault(chassis, set()).add(row["device_id"])
        if name:
            by_name.setdefault(name, set()).add(row["device_id"])

    return by_chassis, by_name
