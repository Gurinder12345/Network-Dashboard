"""
Dell OS10 change safety policy (V1).

The OS10 switches in this lab are the cores, so V1 is an ALLOW-LIST, not a deny-list:

    parent : exactly one  `interface ethernet<unit>/<slot>/<port>[:<breakout>]`
    lines  : `description <text>`   (text: 1-64 of A-Z a-z 0-9 . _ -)
             `no description`

Everything else is rejected. Commands are tokenized and classified so a rejection says
WHY (management, AAA, routing, shutdown, ...), but the decision never depends on the
deny-list being complete: anything not on the allow-list is refused.

Interface names follow the real captures in tests/fixtures/os10_real_*.txt
(e.g. ethernet1/1/5, ethernet1/1/26:2). `interface ethernet 1/1/5` (with a space) is
accepted and canonicalized to `interface ethernet1/1/5`, the form running-configuration
uses. The interface number is never hard-coded; the operator chooses it per change.
"""

import re

ETHERNET_PARENT = re.compile(r"^interface\s+ethernet\s*(\d+/\d+/\d+(?::\d+)?)$", re.IGNORECASE)
DESCRIPTION = re.compile(r"^description\s+(\S+)$", re.IGNORECASE)
DESCRIPTION_TEXT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

def _starts(t, *prefix):
    return t[:len(prefix)] == list(prefix)


def _management(t):
    return any(x.startswith("mgmt") or x == "management" for x in t)


def _ssh(t):
    return "ssh" in t or "ssh-server" in t or _starts(t, "crypto")


def _aaa(t):
    return any(x in ("aaa", "tacacs-server", "radius-server") for x in t[:3])


def _credentials(t):
    return any(x in ("username", "password", "enable", "userrole", "role") for x in t[:3])


def _system(t):
    return bool(t) and (t[0] in ("reload", "reboot", "write", "delete", "copy", "restore", "boot")
                        or "factory-defaults" in t or "startup-configuration" in t)


def _routing(t):
    return (_starts(t, "router") or _starts(t, "no", "router") or _starts(t, "ip", "route")
            or _starts(t, "ipv6", "route") or _starts(t, "no", "ip", "route") or _starts(t, "no", "ipv6", "route"))


def _ip_address(t):
    return "address" in t[:3] and bool(t) and t[0] in ("ip", "ipv6", "no")


def _vlan(t):
    return (_starts(t, "vlan") or _starts(t, "no", "vlan")
            or any(x.startswith("vlan") for x in t[1:3] if t[0] in ("interface", "no")))


def _port_channel(t):
    return any(x.startswith("port-channel") or x == "channel-group" for x in t)


def _switchport(t):
    return "switchport" in t[:2]


def _shutdown(t):
    return t in (["shutdown"], ["no", "shutdown"])


def _spanning_tree(t):
    return "spanning-tree" in t[:2]


# (category, human reason, predicate on the lowercased token list). First match wins.
# Messaging only: the allow-list in check_os10 is what decides.
DENY_RULES = (
    ("management", "management interface / VRF / route changes are not allowed", _management),
    ("ssh", "SSH server/crypto settings are not allowed", _ssh),
    ("aaa", "AAA / TACACS / RADIUS changes are not allowed", _aaa),
    ("credentials", "user / password / role changes are not allowed", _credentials),
    ("system", "reload / reboot / factory-default / file operations are not allowed", _system),
    ("routing", "routing process / static route changes are not allowed", _routing),
    ("ip_address", "IP address changes are not allowed", _ip_address),
    ("vlan", "VLAN creation/removal is not allowed", _vlan),
    ("port_channel", "port-channel / channel-group changes are not allowed", _port_channel),
    ("switchport", "switchport changes are not allowed", _switchport),
    ("shutdown", "shutdown / no shutdown is not allowed", _shutdown),
    ("spanning_tree", "spanning-tree changes are not allowed", _spanning_tree),
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
    return "not_allowed_v1", "only 'description <text>' and 'no description' under one ethernet interface are allowed in V1"


def check_os10(config_lines, config_parents):
    """
    Validate and canonicalize an OS10 change. Returns (lines, parents) in canonical form;
    raises PolicyViolation listing every rejected command.
    """
    violations = []
    lines = [(line or "").strip() for line in (config_lines or [])]
    parents = [(p or "").strip() for p in (config_parents or [])]

    canonical_parents = []
    if len(parents) != 1:
        violations.append({"command": " / ".join(parents) or "(none)", "category": "context",
                           "reason": "exactly one parent `interface ethernetX/Y/Z` is required"})
    else:
        match = ETHERNET_PARENT.match(parents[0])
        if match:
            canonical_parents = [f"interface ethernet{match.group(1)}"]
        else:
            category, reason = _classify(parents[0])
            if category == "not_allowed_v1":
                reason = "the parent must be a front-panel `interface ethernetX/Y/Z`"
            violations.append({"command": parents[0], "category": category if category != "not_allowed_v1" else "context",
                               "reason": reason})

    if not lines:
        violations.append({"command": "(none)", "category": "context", "reason": "at least one command is required"})

    canonical_lines = []
    for line in lines:
        if line.lower() == "no description":
            canonical_lines.append("no description")
            continue
        match = DESCRIPTION.match(line)
        if match or line.lower().startswith("description "):
            text = match.group(1).strip('"') if match else ""
            if text and DESCRIPTION_TEXT.match(text):
                canonical_lines.append(f"description {text}")
                continue
            violations.append({"command": line, "category": "description",
                               "reason": "description text must be 1-64 characters of A-Z a-z 0-9 . _ - (no spaces)"})
            continue
        category, reason = _classify(line)
        violations.append({"command": line, "category": category, "reason": reason})

    if len({line.split()[0] for line in canonical_lines}) < len(canonical_lines) or (
            "no description" in canonical_lines and any(l.startswith("description ") for l in canonical_lines)):
        violations.append({"command": " / ".join(canonical_lines), "category": "context",
                           "reason": "send one description change per request"})

    if violations:
        raise PolicyViolation(violations)
    return canonical_lines, canonical_parents
