"""
Dell OS10 change safety policy (safe L2, V1.1).

The OS10 switches in this lab are the cores, so this is an ALLOW-LIST, not a deny-list:

    parent : exactly one  `interface ethernet<unit>/<slot>/<port>[:<breakout>]`
    lines  : description <text>          (text: 1-64 of A-Z a-z 0-9 . _ -)
             no description
             switchport mode access
             switchport mode trunk
             switchport access vlan <id>
             switchport trunk allowed vlan <list>          (set the allowed list exactly)
             switchport trunk allowed vlan add <list>
             switchport trunk allowed vlan remove <list>
             shutdown
             no shutdown

Everything else is rejected, including global configuration, native VLAN, MTU, and any
management / AAA / routing / VLT / port-channel / ACL / QoS / SNMP command. Commands are
tokenized and classified so a rejection says WHY, but the decision never depends on the
deny-list being complete: anything not on the allow-list is refused.

This module is STATELESS (syntax, normalization, contradictory combinations). Whether the
target interface is safe to modify (LLDP neighbors, port-channel, management path, VLAN
existence, current mode, ...) is decided against live device state in changes/os10_plan.py.

Two grammars:
  * check_os10()          operator request  -> canonical requested lines (intents)
  * check_os10_commands() device commands   -> what an approval stores and Ansible sends.
    The planner derives them from the intents and the device state; they are re-validated
    here before apply. `add` / `remove` never reach the device: OS10's own
    `switchport trunk allowed vlan <list>` adds VLANs and `no switchport trunk allowed vlan
    <list>` removes them.

Interface names follow the real captures in tests/fixtures/os10_real_*.txt
(e.g. ethernet1/1/5, ethernet1/1/26:2). `interface ethernet 1/1/5` (with a space) and any
keyword case are accepted and canonicalized to `interface ethernet1/1/5`, the form
running-configuration uses. Abbreviations (`int eth1/1/5`, `interface Eth1/1/5`) are
rejected. The interface number is never hard-coded; the operator chooses it per change.
"""

import re

from changes.vlans import VlanListError, format_vlan_list, parse_vlan_id, parse_vlan_list

ETHERNET_PARENT = re.compile(r"^interface\s+ethernet\s*(\d+/\d+/\d+(?::\d+)?)$", re.IGNORECASE)
DESCRIPTION = re.compile(r"^description\s+(\S+)$", re.IGNORECASE)
DESCRIPTION_TEXT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_COMMAND_LENGTH = 250

SUPPORTED_SWITCHPORT = ("switchport mode access|trunk, switchport access vlan <id>, "
                        "switchport trunk allowed vlan [add|remove] <list>")


def _starts(t, *prefix):
    return t[:len(prefix)] == list(prefix)


def _management(t):
    return any(x.startswith("mgmt") or x == "management" for x in t)


def _ssh(t):
    return "ssh" in t or "ssh-server" in t or _starts(t, "crypto")


def _aaa(t):
    return any(x in ("aaa", "tacacs-server", "radius-server", "tacacs", "radius") for x in t[:3])


def _credentials(t):
    return any(x in ("username", "password", "enable", "userrole", "role") for x in t[:3])


def _system(t):
    return bool(t) and (t[0] in ("reload", "reboot", "write", "delete", "copy", "restore", "boot", "erase", "image")
                        or "factory-defaults" in t or "factory" in t or "startup-configuration" in t
                        or "startup-config" in t)


def _default(t):
    return _starts(t, "default")


def _interface(t):
    return _starts(t, "interface") or _starts(t, "no", "interface") or _starts(t, "range")


def _routing(t):
    return (_starts(t, "router") or _starts(t, "no", "router") or _starts(t, "ip", "route")
            or _starts(t, "ipv6", "route") or _starts(t, "no", "ip", "route") or _starts(t, "no", "ipv6", "route")
            or any(x in ("bgp", "ospf", "ospfv3", "vrf", "vrrp", "pim") for x in t[:3]))


def _ip_address(t):
    return "address" in t[:3] and bool(t) and t[0] in ("ip", "ipv6", "no")


def _vlt(t):
    return any(x.startswith("vlt") for x in t)


def _vlan(t):
    return (_starts(t, "vlan") or _starts(t, "no", "vlan")
            or any(x.startswith("vlan") for x in t[1:3] if t[0] in ("interface", "no")))


def _port_channel(t):
    return any(x.startswith("port-channel") or x in ("channel-group", "lacp") for x in t)


def _snmp(t):
    return any(x.startswith("snmp") for x in t[:2])


def _acl(t):
    return any(x in ("access-list", "access-group", "acl") for x in t[:3])


def _qos(t):
    return any(x in ("qos", "service-policy", "trust", "class-map", "policy-map", "flowcontrol", "priority-flow-control")
               for x in t[:2])


def _spanning_tree(t):
    return "spanning-tree" in t[:2]


def _mtu(t):
    return _starts(t, "mtu") or _starts(t, "no", "mtu")


def _no_switchport(t):
    return t == ["no", "switchport"]


def _switchport(t):
    return "switchport" in t[:2]


# (category, human reason, predicate on the lowercased token list). First match wins.
# Messaging only: the allow-list in check_os10 is what decides.
DENY_RULES = (
    ("management", "management interface / VRF / route changes are not allowed", _management),
    ("ssh", "SSH server/crypto settings are not allowed", _ssh),
    ("aaa", "AAA / TACACS / RADIUS changes are not allowed", _aaa),
    ("credentials", "user / password / role changes are not allowed", _credentials),
    ("system", "reload / reboot / erase / factory-default / file operations are not allowed", _system),
    ("default", "`default` commands (e.g. default interface) are not allowed", _default),
    ("routing", "routing protocol / VRF / static route changes are not allowed", _routing),
    ("ip_address", "IP address changes are not allowed", _ip_address),
    ("vlt", "VLT configuration is not allowed", _vlt),
    ("vlan", "VLAN creation/removal is not allowed", _vlan),
    ("port_channel", "port-channel / channel-group / LACP changes are not allowed", _port_channel),
    ("interface", "interface creation/removal or a second interface context is not allowed", _interface),
    ("snmp", "SNMP configuration is not allowed", _snmp),
    ("acl", "ACL changes are not allowed", _acl),
    ("qos", "QoS / flow-control changes are not allowed", _qos),
    ("spanning_tree", "spanning-tree changes are not allowed", _spanning_tree),
    ("mtu", "MTU changes are not supported in this release", _mtu),
    ("switchport", "`no switchport` (conversion to a routed port) is not allowed", _no_switchport),
    ("switchport", f"unsupported switchport command; supported: {SUPPORTED_SWITCHPORT}", _switchport),
)


class PolicyViolation(ValueError):
    def __init__(self, violations):
        self.violations = violations
        super().__init__("Rejected by OS10 change policy: " + "; ".join(f"{v['command']!r}: {v['reason']}" for v in violations))


def _classify(command):
    tokens = command.lower().split()
    for category, reason, predicate in DENY_RULES:
        if predicate(tokens):
            return category, reason
    return "not_allowed", ("not in the Dell OS10 safe-L2 allow-list (description, switchport access/trunk, "
                           "allowed VLANs, shutdown / no shutdown on one ethernet interface)")


# ---- operator grammar ---------------------------------------------------------------------
# Intent kinds; at most one of each per request.
KIND_LABELS = {"description": "description", "mode": "switchport mode", "access_vlan": "access VLAN",
               "trunk": "trunk allowed-VLAN", "admin": "shutdown / no shutdown"}
INTENT_ORDER = ("description", "mode", "access_vlan", "trunk", "admin")


def _intent(kind, value, line):
    return {"kind": kind, "value": value, "line": line}


def parse_request_line(line):
    """
    One operator line -> intent dict, or raises ValueError(category, reason).
    Intent values: description text | None (no description); "access"|"trunk";
    VLAN id; ("set"|"add"|"remove", tuple of VLANs); "down"|"up".
    """
    t = line.split()
    low = [x.lower() for x in t]

    if low == ["no", "description"]:
        return _intent("description", None, "no description")
    match = DESCRIPTION.match(line)
    if match or (low and low[0] == "description"):
        text = match.group(1).strip('"') if match else ""
        if text and DESCRIPTION_TEXT.match(text):
            return _intent("description", text, f"description {text}")
        raise _Reject("description", "description text must be 1-64 characters of A-Z a-z 0-9 . _ - (no spaces)")

    if low == ["shutdown"]:
        return _intent("admin", "down", "shutdown")
    if low == ["no", "shutdown"]:
        return _intent("admin", "up", "no shutdown")

    if low[:2] == ["switchport", "mode"] and len(low) == 3 and low[2] in ("access", "trunk"):
        return _intent("mode", low[2], f"switchport mode {low[2]}")

    try:
        if low[:3] == ["switchport", "access", "vlan"] and len(low) == 4:
            vlan = parse_vlan_id(t[3])
            return _intent("access_vlan", vlan, f"switchport access vlan {vlan}")
        if low[:4] == ["switchport", "trunk", "allowed", "vlan"]:
            if len(low) == 5 and low[4] not in ("add", "remove", "all", "none", "except"):
                vlans = parse_vlan_list(t[4])
                return _intent("trunk", ("set", vlans), f"switchport trunk allowed vlan {format_vlan_list(vlans)}")
            if len(low) == 6 and low[4] in ("add", "remove"):
                vlans = parse_vlan_list(t[5])
                return _intent("trunk", (low[4], vlans), f"switchport trunk allowed vlan {low[4]} {format_vlan_list(vlans)}")
            raise _Reject("vlan_syntax", "expected `switchport trunk allowed vlan [add|remove] <list>` "
                                         "with a list such as 10 / 10,20 / 10-20 / 10,20-30,40")
        if low[:3] == ["switchport", "access", "vlan"]:
            raise _Reject("vlan_syntax", "expected `switchport access vlan <id>` with one VLAN ID 1-4094")
    except VlanListError as exc:
        raise _Reject("vlan_syntax", str(exc)) from None

    if low[:3] == ["switchport", "trunk", "native"]:
        raise _Reject("switchport", "native VLAN changes are not supported in this release")
    if low[:2] == ["no", "switchport"] and len(low) > 2:
        raise _Reject("switchport", "`no switchport ...` forms are not accepted; use "
                                    "`switchport trunk allowed vlan remove <list>` to remove VLANs from a trunk")
    raise _Reject(*_classify(line))


class _Reject(ValueError):
    def __init__(self, category, reason):
        self.category, self.reason = category, reason
        super().__init__(reason)


def _combination_violations(intents):
    violations = []
    by_kind = {}
    for intent in intents:
        by_kind.setdefault(intent["kind"], []).append(intent)

    for kind, items in by_kind.items():
        if len(items) > 1:
            violations.append({"command": " / ".join(i["line"] for i in items), "category": "context",
                               "reason": f"send one {KIND_LABELS[kind]} change per request"})

    mode = by_kind.get("mode", [{}])[0].get("value")
    if mode == "access" and "trunk" in by_kind:
        violations.append({"command": f"switchport mode access / {by_kind['trunk'][0]['line']}", "category": "context",
                           "reason": "contradictory: a trunk allowed-VLAN change on a port being set to access mode"})
    if mode == "trunk" and "access_vlan" in by_kind:
        violations.append({"command": f"switchport mode trunk / {by_kind['access_vlan'][0]['line']}", "category": "context",
                           "reason": "on OS10 `switchport access vlan` on a trunk sets its untagged (native) VLAN; "
                                     "native VLAN changes are not supported in this release"})
    if mode is None and "access_vlan" in by_kind and "trunk" in by_kind:
        violations.append({"command": f"{by_kind['access_vlan'][0]['line']} / {by_kind['trunk'][0]['line']}",
                           "category": "context",
                           "reason": "ambiguous: an access VLAN and a trunk allowed-VLAN change in one request"})
    return violations


def _check_parent(parents):
    if len(parents) != 1:
        return None, {"command": " / ".join(parents) or "(none)", "category": "context",
                      "reason": "exactly one parent `interface ethernetX/Y/Z` is required "
                                "(global configuration is not allowed)"}
    match = ETHERNET_PARENT.match(parents[0])
    if match:
        return [f"interface ethernet{match.group(1)}"], None
    category, reason = _classify(parents[0])
    if category in ("not_allowed", "interface"):
        category, reason = "context", "the parent must be a front-panel `interface ethernetX/Y/Z`"
    return None, {"command": parents[0], "category": category, "reason": reason}


def parse_intents(lines):
    """Canonical requested lines -> intent dicts (raises PolicyViolation)."""
    intents, violations = [], []
    for line in lines:
        try:
            intents.append(parse_request_line(line))
        except _Reject as exc:
            violations.append({"command": line, "category": exc.category, "reason": exc.reason})
    if violations:
        raise PolicyViolation(violations)
    return intents


def check_os10(config_lines, config_parents):
    """
    Validate and canonicalize an operator's OS10 change request. Returns (lines, parents)
    in canonical form (one line per intent, fixed order); raises PolicyViolation listing
    every rejected command.
    """
    violations = []
    lines = [(line or "").strip() for line in (config_lines or [])]
    parents = [(p or "").strip() for p in (config_parents or [])]

    canonical_parents, parent_violation = _check_parent(parents)
    if parent_violation:
        violations.append(parent_violation)

    if not lines:
        violations.append({"command": "(none)", "category": "context", "reason": "at least one command is required"})

    intents = []
    for line in lines:
        try:
            intents.append(parse_request_line(line))
        except _Reject as exc:
            violations.append({"command": line, "category": exc.category, "reason": exc.reason})

    violations += _combination_violations(intents)
    if violations:
        raise PolicyViolation(violations)

    intents.sort(key=lambda i: INTENT_ORDER.index(i["kind"]))
    return [i["line"] for i in intents], canonical_parents


# ---- device grammar -------------------------------------------------------------------------
# What the planner emits and an approval stores, in this order. `no switchport trunk allowed
# vlan` comes before `switchport mode access` (trunk -> access) and after the allowed list
# otherwise (add first, then remove: VLANs that stay are never dropped in between).
_DEVICE_PATTERNS = (
    ("shutdown", re.compile(r"^shutdown$")),
    ("description", re.compile(r"^description ([A-Za-z0-9._-]{1,64})$")),
    ("no_description", re.compile(r"^no description$")),
    ("mode", re.compile(r"^switchport mode (access|trunk)$")),
    ("access_vlan", re.compile(r"^switchport access vlan (\d+)$")),
    ("trunk_add", re.compile(r"^switchport trunk allowed vlan ([0-9,-]+)$")),
    ("trunk_remove", re.compile(r"^no switchport trunk allowed vlan ([0-9,-]+)$")),
    ("no_shutdown", re.compile(r"^no shutdown$")),
)


def parse_device_command(command):
    """Canonical device command -> (kind, value), or None if it is not exactly canonical."""
    for kind, pattern in _DEVICE_PATTERNS:
        match = pattern.match(command)
        if not match:
            continue
        value = match.group(1) if match.groups() else None
        try:
            if kind == "access_vlan":
                value = parse_vlan_id(value)
                if command != f"switchport access vlan {value}":
                    return None
            elif kind in ("trunk_add", "trunk_remove"):
                vlans = parse_vlan_list(value, device_limits=True)
                if value != format_vlan_list(vlans):
                    return None
                value = vlans
        except VlanListError:
            return None
        return kind, value
    return None


def _device_rank(kind, kinds):
    if kind == "trunk_remove" and ("mode", "access") in kinds:
        return 2
    return {"shutdown": 0, "description": 1, "no_description": 1, "mode": 3, "access_vlan": 4, "trunk_add": 5,
            "trunk_remove": 6, "no_shutdown": 7}[kind]


def check_os10_commands(config_lines, config_parents):
    """
    Validate stored device commands (an approval) before they are sent. They must be
    exactly what the planner emits: canonical text, known kinds, each at most once, in
    planner order, with no contradictions. Returns (lines, parents); raises PolicyViolation.
    """
    violations = []
    lines = list(config_lines or [])
    parents = list(config_parents or [])

    canonical_parents, parent_violation = _check_parent(parents)
    if parent_violation or canonical_parents != parents:
        violations.append(parent_violation or {"command": parents[0], "category": "context",
                                               "reason": "stored parent is not in canonical form"})
    if not lines:
        violations.append({"command": "(none)", "category": "context", "reason": "at least one command is required"})

    parsed = []
    for line in lines:
        result = parse_device_command(line) if isinstance(line, str) and len(line) <= MAX_COMMAND_LENGTH else None
        if result is None:
            category, reason = _classify(line if isinstance(line, str) else "")
            violations.append({"command": line, "category": category if category != "not_allowed" else "context",
                               "reason": f"not a canonical safe-L2 device command ({reason})"})
        else:
            parsed.append(result)

    if not violations:
        kinds = [kind for kind, _ in parsed]
        pairs = {(kind, value) for kind, value in parsed if kind == "mode"}
        if len(set(kinds)) != len(kinds):
            violations.append({"command": " / ".join(lines), "category": "context", "reason": "duplicate command kinds"})
        conflicts = ({"shutdown", "no_shutdown"}, {"description", "no_description"}, {"access_vlan", "trunk_add"})
        if any(pair <= set(kinds) for pair in conflicts) or (("mode", "access") in pairs and "trunk_add" in kinds) \
                or (("mode", "trunk") in pairs and "access_vlan" in kinds):
            violations.append({"command": " / ".join(lines), "category": "context", "reason": "contradictory commands"})
        ranks = [_device_rank(kind, pairs) for kind in kinds]
        if ranks != sorted(ranks):
            violations.append({"command": " / ".join(lines), "category": "context",
                               "reason": "commands are not in the planned order"})

    if violations:
        raise PolicyViolation(violations)
    return lines, parents


# ---- shared ---------------------------------------------------------------------------------
def change_kinds(lines):
    """Kinds of change in canonical lines of either grammar: description, switchport, shutdown, no_shutdown."""
    kinds = set()
    for line in lines or []:
        low = line.lower().split()
        if low == ["shutdown"]:
            kinds.add("shutdown")
        elif low == ["no", "shutdown"]:
            kinds.add("no_shutdown")
        elif "switchport" in low[:2]:
            kinds.add("switchport")
        else:
            kinds.add("description")
    return kinds
