"""
Dell OS10 running-configuration parsing (pure functions; no device, DB or Vault access).
Used by the semantic verifier (changes/verify.py) for interface switchport state and by the
read-only live validator.

OS10 running-configuration lists each interface as a top-level block:

    interface ethernet1/1/5
     description AUTOMATION-TEST
     no shutdown
     switchport mode trunk
     switchport access vlan 1
     switchport trunk allowed vlan 10,20-22
    !

How switchport state is read (nothing is assumed when it is not printed):
  admin        `shutdown` -> down, `no shutdown` -> up; neither or both -> unknown
  mode         `no switchport` / `ip address` -> routed; `switchport mode trunk|access`;
               otherwise `switchport access vlan N` with no trunk lines -> access
               (OS10 omits the access-mode default); anything else -> unknown
  access VLAN  `switchport access vlan N`; on a trunk this is its untagged VLAN
  allowed      union of `switchport trunk allowed vlan <list>` lines
Unrecognised `switchport ...` lines or conflicting lines make the field unknown.
"""

import re

from changes.vlans import VlanListError, parse_vlan_list


class VerificationError(RuntimeError):
    """Device output could not be interpreted safely. The message never contains secrets."""


# ---- block parsing ---------------------------------------------------------------------------
def _blocks(running_config):
    """Top-level `interface <name>` / `vlt-domain` blocks: {lowercased header: [body lines]}."""
    blocks, header = {}, None
    for line in running_config.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            if header is not None:
                blocks[header].append(line.strip())
            continue
        header = None
        stripped = line.strip()
        if stripped == "!":
            continue
        if stripped.lower().startswith(("interface ", "vlt-domain")):
            header = " ".join(stripped.lower().split())
            blocks.setdefault(header, [])
    return blocks


def interface_block(running_config, parent):
    lines = running_config.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == parent.lower() and not line.startswith((" ", "\t")):
            block = []
            for body in lines[index + 1:]:
                if body.strip() == "!" or (body.strip() and not body.startswith((" ", "\t"))):
                    break
                if body.strip():
                    block.append(body.strip())
            return block
    return None


def _check_recognisable(running_config):
    if not running_config or "interface ethernet" not in running_config:
        raise VerificationError("Running configuration was empty or not recognisable as Dell OS10 output")


def _description(block):
    for line in block:
        if line.lower().startswith("description "):
            value = line[len("description "):].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            return value
    return None


def parse_interface(block, name):
    """Interface body lines -> state dict (see module docstring)."""
    state = {"interface": name, "description": _description(block), "admin": None, "mode": None,
             "access_vlan": None, "allowed_vlans": (), "port_channel": None, "ip_addresses": [],
             "routed": False, "admin_issues": [], "switchport_issues": []}
    admin, modes, access, allowed = set(), set(), set(), set()
    trunk_lines = 0

    for line in block:
        low = line.lower().split()
        if low == ["shutdown"]:
            admin.add("down")
        elif low == ["no", "shutdown"]:
            admin.add("up")
        elif low == ["no", "switchport"]:
            state["routed"] = True
        elif low[:2] == ["ip", "address"] or low[:2] == ["ipv6", "address"]:
            state["routed"] = True
            state["ip_addresses"].append(" ".join(low[2:]))
        elif low[:1] == ["channel-group"] and len(low) >= 2:
            state["port_channel"] = low[1]
        elif low[:2] == ["switchport", "mode"] and len(low) == 3 and low[2] in ("access", "trunk"):
            modes.add(low[2])
        elif low[:3] == ["switchport", "access", "vlan"] and len(low) == 4 and low[3].isdigit():
            access.add(int(low[3]))
        elif low[:4] == ["switchport", "trunk", "allowed", "vlan"] and len(low) == 5:
            trunk_lines += 1
            try:
                allowed.update(parse_vlan_list(low[4], device_limits=True))
            except VlanListError:
                state["switchport_issues"].append("unparseable trunk allowed-VLAN list")
        elif "switchport" in low[:2]:
            state["switchport_issues"].append(f"unrecognised switchport line ({' '.join(low[:4])})")

    if len(admin) == 1:
        state["admin"] = admin.pop()
    elif admin:
        state["admin_issues"].append("both `shutdown` and `no shutdown` present")
    else:
        state["admin_issues"].append("admin state (shutdown / no shutdown) not shown")

    if len(access) == 1:
        state["access_vlan"] = next(iter(access))
    elif access:
        state["switchport_issues"].append("more than one access VLAN line")
    state["allowed_vlans"] = tuple(sorted(allowed))

    if len(modes) > 1:
        state["switchport_issues"].append("conflicting switchport mode lines")
    elif state["routed"]:
        if modes or access or trunk_lines:
            state["switchport_issues"].append("routed interface also shows switchport lines")
        else:
            state["mode"] = "routed"
    elif modes == {"trunk"}:
        state["mode"] = "trunk"
    elif trunk_lines:
        state["switchport_issues"].append("trunk allowed-VLAN lines on a port not in trunk mode")
    elif modes == {"access"} or state["access_vlan"] is not None:
        state["mode"] = "access"
    else:
        state["switchport_issues"].append("switchport mode not shown")

    if state["switchport_issues"]:
        state["mode"] = None
    return state


def interface_state(running_config, parent):
    _check_recognisable(running_config)
    block = interface_block(running_config, parent)
    if block is None:
        raise VerificationError(f"{parent} was not found in the running configuration")
    state = parse_interface(block, parent.split(None, 1)[1])
    state["block"] = block
    return state


def existing_vlans(running_config):
    """VLAN IDs that exist on the device: top-level `interface vlanN` blocks."""
    vlans = set()
    for header in _blocks(running_config):
        match = re.fullmatch(r"interface vlan(\d+)", header)
        if match and 1 <= int(match.group(1)) <= 4094:
            vlans.add(int(match.group(1)))
    return vlans
