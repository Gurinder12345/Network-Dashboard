"""
Dell OS10 running-configuration parsing and verification for the safe-L2 command set.
Pure functions (no device, DB or Vault access).

Used by precheck (current state), pre-apply (still needed? still safe?) and post-check
(did the device take it?). verify() output has the same shape as the OS6 verifier
(ansible/run_os6.py verify_config_commands), so the shared workflow and the UI treat both
platforms identically.

OS10 running-configuration lists each interface as a top-level block:

    interface ethernet1/1/5
     description AUTOMATION-TEST
     no shutdown
     switchport mode trunk
     switchport access vlan 1
     switchport trunk allowed vlan 10,20-22
    !

The block starts at the exact `interface <name>` line and ends at the next `!` or the
next non-indented line. A missing interface is an error, never "no change".

How state is read (nothing is assumed when it is not printed):
  admin        `shutdown` -> down, `no shutdown` -> up; neither or both -> unknown
  mode         `no switchport` / `ip address` -> routed; `switchport mode trunk|access`;
               otherwise `switchport access vlan N` with no trunk lines -> access
               (OS10 omits the access-mode default); anything else -> unknown
  access VLAN  `switchport access vlan N`; on a trunk this is its untagged VLAN
  allowed      union of `switchport trunk allowed vlan <list>` lines
  port-channel `channel-group N ...`
Unrecognised `switchport ...` lines or conflicting lines make the affected field unknown
(and are reported as issues) so callers fail safely instead of guessing.
"""

import ipaddress
import re

from changes.vlans import VlanListError, format_vlan_list, parse_vlan_list

VERIFICATION_METHOD = "running-configuration"


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


def management_location(running_config, management_ip):
    """
    Where the device's management IP lives. Returns {"kind": out_of_band | vlan | interface |
    unknown, ...}. Only an exact address match (or DHCP on mgmt1/1/1 with no other DHCP
    interface) counts; anything else is "unknown".
    """
    try:
        target = ipaddress.ip_address(str(management_ip or "").split("/")[0])
    except ValueError:
        return {"kind": "unknown", "detail": "device management IP is not known"}

    hits, dhcp = [], []
    for header, body in _blocks(running_config).items():
        if not header.startswith("interface "):
            continue
        name = header.split(None, 1)[1]
        for line in body:
            low = line.lower().split()
            if low[:2] != ["ip", "address"] or len(low) < 3:
                continue
            if low[2] == "dhcp":
                dhcp.append(name)
                continue
            try:
                if ipaddress.ip_interface(low[2]).ip == target:
                    hits.append(name)
            except ValueError:
                continue

    if len(hits) == 1:
        name = hits[0]
        if name.startswith("mgmt"):
            return {"kind": "out_of_band", "interface": name, "detail": f"management IP is on {name} (out-of-band)"}
        if re.fullmatch(r"vlan\d+", name):
            vlan = int(name[4:])
            return {"kind": "vlan", "vlan": vlan, "detail": f"management IP is on in-band VLAN {vlan}"}
        if name.startswith("ethernet"):
            return {"kind": "interface", "interface": name, "detail": f"management IP is on routed port {name}"}
        return {"kind": "unknown", "detail": f"management IP is on {name}; its physical path cannot be determined"}
    if len(hits) > 1:
        return {"kind": "unknown", "detail": "management IP appears on more than one interface"}
    if dhcp == ["mgmt1/1/1"]:
        return {"kind": "out_of_band", "interface": "mgmt1/1/1",
                "detail": "management address is DHCP on mgmt1/1/1 (out-of-band); no other interface uses DHCP"}
    return {"kind": "unknown", "detail": "management IP not found in the running configuration"}


_VLT_ITEM = re.compile(r"^ethernet(\d+/\d+/)(\d+)(?::(\d+))?(?:-(?:(\d+/\d+/))?(\d+))?$")


def vlt_interfaces(running_config):
    """
    Ethernet ports listed as VLT discovery (VLTi) interfaces. Returns (set, error or None);
    an unparseable vlt-domain makes VLT membership unknown for every port.
    """
    members = set()
    for header, body in _blocks(running_config).items():
        if not header.startswith("vlt-domain"):
            continue
        for line in body:
            low = line.lower().split()
            if low[:1] != ["discovery-interface"]:
                continue
            if len(low) != 2:
                return members, "unrecognised VLT discovery-interface line"
            for item in low[1].split(","):
                match = _VLT_ITEM.match(item)
                if not match or match.group(3) and match.group(5):
                    return members, f"unrecognised VLT discovery-interface item {item!r}"
                prefix, start, breakout, end_prefix, end = match.groups()
                if end is None:
                    members.add(f"ethernet{prefix}{start}" + (f":{breakout}" if breakout else ""))
                    continue
                if end_prefix not in (None, prefix) or int(end) < int(start):
                    return members, f"unrecognised VLT discovery-interface range {item!r}"
                members.update(f"ethernet{prefix}{port}" for port in range(int(start), int(end) + 1))
    return members, None


# ---- verification of device commands (postconditions) -----------------------------------
def _postcondition(kind, value, state):
    """(desired_state_present, current_value) for one canonical device command."""
    if kind == "description":
        return state["description"] == value, state["description"]
    if kind == "no_description":
        return state["description"] is None, state["description"]
    if kind in ("shutdown", "no_shutdown"):
        if state["admin"] is None:
            raise VerificationError(f"cannot determine admin state of {state['interface']}: "
                                    + "; ".join(state["admin_issues"]))
        return state["admin"] == ("down" if kind == "shutdown" else "up"), state["admin"]

    if state["mode"] is None:
        raise VerificationError(f"cannot determine switchport state of {state['interface']}: "
                                + "; ".join(state["switchport_issues"]))
    allowed = set(state["allowed_vlans"])
    if kind == "mode":
        return state["mode"] == value, state["mode"]
    if kind == "access_vlan":
        return state["mode"] == "access" and state["access_vlan"] == value, (
            f"{state['mode']}, access VLAN {state['access_vlan']}")
    if kind == "trunk_add":
        return state["mode"] == "trunk" and set(value) <= allowed, format_vlan_list(allowed) or "none"
    if kind == "trunk_remove":
        return not (set(value) & allowed), format_vlan_list(allowed) or "none"
    raise VerificationError(f"no verifier for {kind}")


COMMAND_TYPES = {"description": "interface_description", "no_description": "interface_description",
                 "mode": "switchport_mode", "access_vlan": "access_vlan", "trunk_add": "trunk_allowed_vlans",
                 "trunk_remove": "trunk_allowed_vlans", "shutdown": "admin_state", "no_shutdown": "admin_state"}


def verify(config_lines, config_parents, running_config):
    """
    config_lines are canonical device commands (changes.policy.check_os10_commands);
    each is checked independently against the interface's current state.
    """
    from changes.policy import parse_device_command

    state = interface_state(running_config, config_parents[0])
    already_present, proposed, results = [], [], []
    for command in config_lines:
        parsed = parse_device_command(command)
        if parsed is None:
            raise VerificationError(f"not a canonical device command: {command!r}")
        present, current = _postcondition(*parsed, state)
        (already_present if present else proposed).append(command)
        results.append({
            "command": command,
            "type": COMMAND_TYPES[parsed[0]],
            "verification_method": VERIFICATION_METHOD,
            "desired_state_present": present,
            "config_parents": config_parents,
            "current_value": current,
        })

    return {
        "already_present": already_present,
        "proposed_changes": proposed,
        "would_change": bool(proposed),
        "verification_method": VERIFICATION_METHOD,
        "command_results": results,
    }
