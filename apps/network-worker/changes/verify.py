"""
Semantic verification of configuration-block commands against the running configuration
(Dell OS6 and Dell OS10). Pure functions: the caller supplies the device output.

Every command gets one of three results; nothing is guessed:

    verified       the desired state is visible on the device
    not_present    a command this module understands, and the device state differs
    not_available  no deterministic check exists for this command (arbitrary CLI); it is
                   executed as written and the device remains the syntax authority

Deterministic checks:
  * interface parents: description <text>, no description, shutdown, no shutdown
  * Dell OS10 ethernet interfaces: switchport mode access|trunk, switchport access vlan N,
    [no] switchport trunk allowed vlan <list>  (parsed with changes.os10.parse_interface)
  * Dell OS6 global `vlan N` / `no vlan N` (from `show vlan`)
  * `no <X>` anywhere: verified when the exact positive line <X> is absent from the
    block's running configuration, not_present when it is still there
  * any other command: verified only when the identical line is present in the block's
    running configuration; otherwise not_available (devices often render CLI differently,
    so absence proves nothing)

Scope: the parent's block in running-config (OS10: indented lines up to `!`; OS6: lines up
to `exit`), or the whole configuration for global blocks.
"""

import re

from changes.os10 import parse_interface
from changes.vlans import VlanListError, parse_vlan_list

VERIFIED, NOT_PRESENT, NOT_AVAILABLE = "verified", "not_present", "not_available"
NOT_AVAILABLE_TEXT = "Command accepted for execution; semantic pre-validation is not available for this command."
MAX_CAPTURED_LINES = 200

_OS6_VLAN = re.compile(r"^(no\s+)?vlan\s+(\d{1,4})$", re.IGNORECASE)
_OS10_PHYSICAL = re.compile(r"^interface(ethernet|mgmt)")


def _key(text):
    """Whitespace-free lowercase key: `interface ethernet 1/1/5` == `interface ethernet1/1/5`."""
    return "".join((text or "").lower().split())


def normalize_line(line):
    text = " ".join(line.split())
    match = re.match(r"(?i)^description\s+(.*)$", text)
    if match:
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        text = f"description {value}"
    return text


def scope(platform, running_config, parent):
    """(stripped body lines, found). parent None -> the whole configuration (global)."""
    lines = (running_config or "").splitlines()
    if parent is None:
        if platform == "dell_os10":
            body = [l.strip() for l in lines if l.strip() and l.strip() != "!" and not l.startswith((" ", "\t"))]
        else:
            body = [l.strip() for l in lines if l.strip()]
        return body, True

    want = _key(parent)
    for index, line in enumerate(lines):
        if _key(line) != want or (platform == "dell_os10" and line.startswith((" ", "\t"))):
            continue
        body = []
        for following in lines[index + 1:]:
            stripped = following.strip()
            if platform == "dell_os10":
                if stripped == "!" or (stripped and not following.startswith((" ", "\t"))):
                    break
            elif stripped == "exit":
                break
            if stripped:
                body.append(stripped)
        return body, True
    return [], False


def needs_vlan_table(platform, blocks):
    return platform == "dell_os6" and any(
        block["parent"] is None and any(_OS6_VLAN.match(" ".join(c.split())) for c in block["commands"])
        for block in blocks)


def parse_os6_vlan_ids(show_vlan_output):
    vlans = set()
    for line in (show_vlan_output or "").splitlines():
        fields = line.split()
        if fields and fields[0].isdigit():
            vlans.add(int(fields[0]))
    return vlans


def _result(command, status, method, detail):
    return {"command": command, "status": status, "method": method,
            "detail": detail if status != NOT_AVAILABLE or detail else NOT_AVAILABLE_TEXT}


def _description(body):
    for line in body:
        normalized = normalize_line(line)
        if normalized.lower().startswith("description "):
            return normalized[len("description "):]
    return None


def _os10_switchport(command, body):
    low = " ".join(command.lower().split())
    mode = re.fullmatch(r"switchport mode (access|trunk)", low)
    access = re.fullmatch(r"switchport access vlan (\d{1,4})", low)
    trunk = re.fullmatch(r"(no )?switchport trunk allowed vlan ([0-9,\-]+)", low)
    if not (mode or access or trunk):
        return None
    state = parse_interface(body, "interface")
    if state["mode"] is None:
        return NOT_AVAILABLE, "switchport state not determinable: " + "; ".join(state["switchport_issues"])
    if mode:
        return (VERIFIED if state["mode"] == mode.group(1) else NOT_PRESENT), f"mode is {state['mode']}"
    if access:
        ok = state["mode"] == "access" and state["access_vlan"] == int(access.group(1))
        return (VERIFIED if ok else NOT_PRESENT), f"{state['mode']}, access VLAN {state['access_vlan']}"
    try:
        wanted = set(parse_vlan_list(trunk.group(2), device_limits=True))
    except VlanListError:
        return NOT_AVAILABLE, "VLAN list not parseable for verification"
    allowed = set(state["allowed_vlans"])
    current = ",".join(map(str, sorted(allowed))) or "none"
    if trunk.group(1):
        return (VERIFIED if not wanted & allowed else NOT_PRESENT), f"allowed VLANs {current}"
    ok = state["mode"] == "trunk" and wanted <= allowed
    return (VERIFIED if ok else NOT_PRESENT), f"allowed VLANs {current}"


def verify_command(platform, parent, command, body, found, vlan_ids=None):
    normalized = normalize_line(command)
    low = normalized.lower()
    is_interface = bool(parent) and _key(parent).startswith("interface")
    method = "running-configuration"

    vlan = _OS6_VLAN.match(normalized)
    if platform == "dell_os6" and parent is None and vlan and vlan_ids is not None:
        present = int(vlan.group(2)) in vlan_ids
        wanted = not vlan.group(1)
        return _result(command, VERIFIED if present == wanted else NOT_PRESENT, "show vlan",
                       f"VLAN {vlan.group(2)} {'exists' if present else 'does not exist'}")

    if is_interface:
        current = _description(body)
        if low.startswith("description "):
            wanted = normalized[len("description "):]
            return _result(command, VERIFIED if current == wanted else NOT_PRESENT, method,
                           f"current description: {current or 'none'}")
        if low == "no description":
            return _result(command, VERIFIED if current is None else NOT_PRESENT, method,
                           f"current description: {current or 'none'}")
        shut = "shutdown" in body
        if low == "shutdown":
            return _result(command, VERIFIED if shut else NOT_PRESENT, method, "admin down" if shut else "admin up")
        if low == "no shutdown":
            return _result(command, NOT_PRESENT if shut else VERIFIED, method, "admin down" if shut else "admin up")
        if platform == "dell_os10" and _key(parent).startswith("interfaceethernet") and found:
            switchport = _os10_switchport(normalized, body)
            if switchport:
                return _result(command, switchport[0], method, switchport[1])

    if low.startswith("no ") and len(low) > 3:
        positive = normalized[3:].strip()
        still = any(normalize_line(line) == positive for line in body)
        return _result(command, NOT_PRESENT if still else VERIFIED, method,
                       f"`{positive}` is {'still' if still else 'not'} configured")

    if any(normalize_line(line) == normalized for line in body):
        return _result(command, VERIFIED, method, "line present in running configuration")
    return _result(command, NOT_AVAILABLE, method, None)


def verify_blocks(platform, blocks, running_config, vlan_ids=None, only=None):
    """Per-block verification. `only`: block indexes to verify (others are skipped)."""
    report = []
    for index, block in enumerate(blocks):
        body, found = scope(platform, running_config, block["parent"])
        entry = {"index": index, "parent": block["parent"], "found": found,
                 "current_config": body[:MAX_CAPTURED_LINES], "commands": []}
        if only is None or index in only:
            entry["commands"] = [verify_command(platform, block["parent"], c, body, found, vlan_ids)
                                 for c in block["commands"]]
        report.append(entry)
    return report


def semantic_status(results):
    """Overall status over command results: verified | failed | partial | not_available."""
    statuses = {r["status"] for r in results}
    if not statuses:
        return NOT_AVAILABLE
    if NOT_PRESENT in statuses:
        return "failed"
    if statuses == {VERIFIED}:
        return VERIFIED
    if statuses == {NOT_AVAILABLE}:
        return NOT_AVAILABLE
    return "partial"


def precheck_block_issues(platform, block, found):
    """Problems detectable without changing configuration (block-level FAIL)."""
    if platform == "dell_os10" and block["parent"] and _OS10_PHYSICAL.match(_key(block["parent"])) and not found:
        # OS10 running-configuration lists every physical port, so a missing one does not exist.
        return [f"{block['parent']} was not found on the device"]
    return []
