"""
Strict VLAN ID / VLAN list parsing. Pure functions. Used by the semantic verifier to read
Dell OS10 `switchport trunk allowed vlan <list>` state (device_limits=True) and by the
simulated test switches. Not a command policy.

Accepted list syntax (no spaces):   10   10,20,30   10-20   10,20-30,40
VLAN IDs are 1-4094. Ranges must be ascending. Anything else raises VlanListError.
"""

import re

MIN_VLAN = 1
MAX_VLAN = 4094

# Operator-input limits.
MAX_LIST_TEXT = 200
MAX_LIST_ITEMS = 32
MAX_LIST_VLANS = 256

_ID = r"(?:[1-9][0-9]{0,3})"
_ITEM = re.compile(rf"^({_ID})(?:-({_ID}))?$")


class VlanListError(ValueError):
    pass


def _vlan(text):
    value = int(text)
    if not MIN_VLAN <= value <= MAX_VLAN:
        raise VlanListError(f"VLAN {value} is out of range ({MIN_VLAN}-{MAX_VLAN})")
    return value


def parse_vlan_id(text):
    text = (text or "").strip()
    if not re.fullmatch(_ID, text):
        raise VlanListError(f"invalid VLAN ID {text!r} (expected a number {MIN_VLAN}-{MAX_VLAN})")
    return _vlan(text)


def parse_vlan_list(text, device_limits=False):
    """Returns a sorted tuple of unique VLAN IDs, or raises VlanListError."""
    text = (text or "").strip()
    if not text:
        raise VlanListError("VLAN list is empty")
    if not device_limits and len(text) > MAX_LIST_TEXT:
        raise VlanListError(f"VLAN list is longer than {MAX_LIST_TEXT} characters")

    items = text.split(",")
    if not device_limits and len(items) > MAX_LIST_ITEMS:
        raise VlanListError(f"VLAN list has more than {MAX_LIST_ITEMS} items")

    vlans = set()
    for item in items:
        match = _ITEM.match(item)
        if not match:
            raise VlanListError(f"malformed VLAN list item {item!r} in {text!r}")
        start = _vlan(match.group(1))
        end = _vlan(match.group(2)) if match.group(2) else start
        if match.group(2) and end <= start:
            raise VlanListError(f"VLAN range {item!r} must be ascending")
        vlans.update(range(start, end + 1))
        if not device_limits and len(vlans) > MAX_LIST_VLANS:
            raise VlanListError(f"VLAN list expands to more than {MAX_LIST_VLANS} VLANs")
    return tuple(sorted(vlans))


def format_vlan_list(vlans):
    """Canonical text: sorted, consecutive IDs merged into ranges (10,20-22,40)."""
    ordered = sorted(set(vlans))
    if not ordered:
        return ""
    parts, start, previous = [], ordered[0], ordered[0]
    for vlan in ordered[1:] + [None]:
        if vlan is not None and vlan == previous + 1:
            previous = vlan
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        if vlan is not None:
            start = previous = vlan
    return ",".join(parts)
