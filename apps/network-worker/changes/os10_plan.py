"""
Dell OS10 safe-L2 planner: requested lines (intents) + current device state -> the exact,
ordered device commands an approval will store, plus a human-readable plan. Pure.

    plan()                  precheck: safety, stateful checks, VLAN existence, idempotency,
                            device commands, change plan, expected diff
    device_preconditions()  pre-apply: are the stored commands still valid for the device
                            as it is now? (state may have changed since the precheck)

Device command order (shutdown first, no shutdown last; VLANs that stay on a trunk are
never removed in between):

    shutdown
    description <text> | no description
    no switchport trunk allowed vlan <all current>   (trunk -> access only)
    switchport mode access|trunk
    switchport access vlan <id>
    switchport trunk allowed vlan <resulting list>   (only when VLANs are added)
    no switchport trunk allowed vlan <removed>       (only when VLANs are removed)
    no shutdown

`switchport trunk allowed vlan` always carries the full resulting list: OS10 treats the
command as additive, and sending the full list also gives the right result if it replaced
the list instead; the post-check confirms every VLAN either way.

On OS10 the access VLAN of a trunk port is its untagged VLAN. Native VLAN changes are not
supported in this release, so it is never changed on a trunk and never also tagged.
"""

from changes import os10_safety
from changes.os10 import VERIFICATION_METHOD, existing_vlans, interface_state
from changes.policy import (
    MAX_COMMAND_LENGTH,
    PolicyViolation,
    change_kinds,
    check_os10_commands,
    parse_device_command,
    parse_intents,
)
from changes.vlans import format_vlan_list

MAX_LISTED_VLANS = 10


def _fmt(vlans):
    return format_vlan_list(vlans) or "none"


def _missing_vlan_reasons(vlans, present):
    missing = sorted(set(vlans) - present)
    reasons = [f"VLAN {vlan} does not exist on device." for vlan in missing[:MAX_LISTED_VLANS]]
    if len(missing) > MAX_LISTED_VLANS:
        reasons.append(f"... and {len(missing) - MAX_LISTED_VLANS} more VLANs do not exist on device.")
    return reasons


def _current_allowed(state):
    return set(state["allowed_vlans"]) if state["mode"] == "trunk" else set()


def _simulate(state, intents):
    """Final interface state after the intents, plus stateful rejection reasons."""
    name = state["interface"]
    final = {"description": state["description"], "admin": state["admin"], "mode": state["mode"],
             "access_vlan": state["access_vlan"], "allowed": _current_allowed(state)}
    reasons = []
    by_kind = {i["kind"]: i for i in intents}

    if "description" in by_kind:
        final["description"] = by_kind["description"]["value"]
    if "admin" in by_kind:
        final["admin"] = by_kind["admin"]["value"]

    switchport = any(k in by_kind for k in ("mode", "access_vlan", "trunk"))
    if switchport and state["mode"] not in ("access", "trunk"):
        return final, reasons  # the switchport_state / routed safety checks already reject it

    if "mode" in by_kind:
        final["mode"] = by_kind["mode"]["value"]

    if "access_vlan" in by_kind:
        if final["mode"] != "access":
            reasons.append(f"switchport access vlan rejected: {name} is a trunk port; on OS10 `switchport access vlan` "
                           f"on a trunk sets its untagged (native) VLAN, which is not supported in this release. "
                           f"Add `switchport mode access` to convert the port.")
        else:
            final["access_vlan"] = by_kind["access_vlan"]["value"]

    if "trunk" in by_kind:
        operation, vlans = by_kind["trunk"]["value"]
        if final["mode"] != "trunk":
            reasons.append(f"trunk allowed-VLAN change rejected: {name} is an access port. "
                           f"Add `switchport mode trunk` to convert it.")
        else:
            current = _current_allowed(state)
            final["allowed"] = (set(vlans) if operation == "set" else
                                current | set(vlans) if operation == "add" else current - set(vlans))

    if final["mode"] == "access":
        final["allowed"] = set()
    if final["mode"] == "trunk":
        added = final["allowed"] - _current_allowed(state)
        untagged = final["access_vlan"]
        if added and untagged is None:
            reasons.append(f"trunk allowed-VLAN change rejected: {os10_safety.UNSAFE} "
                           f"(the untagged VLAN of {name} is not shown in the running configuration)")
        elif untagged in added:
            reasons.append(f"VLAN {untagged} is the untagged VLAN of {name}; it cannot also be allowed tagged. "
                           f"Changing the native VLAN is not supported in this release.")
    return final, reasons


def _commands(state, final):
    commands = []
    if final["admin"] == "down" and state["admin"] != "down":
        commands.append("shutdown")
    if final["description"] != state["description"]:
        commands.append(f"description {final['description']}" if final["description"] else "no description")

    current = _current_allowed(state)
    if final["mode"] != state["mode"]:
        if state["mode"] == "trunk" and final["mode"] == "access" and current:
            commands.append(f"no switchport trunk allowed vlan {format_vlan_list(current)}")
        commands.append(f"switchport mode {final['mode']}")
    if final["mode"] == "access" and final["access_vlan"] != state["access_vlan"]:
        commands.append(f"switchport access vlan {final['access_vlan']}")
    if final["mode"] == "trunk":
        if final["allowed"] - current:
            commands.append(f"switchport trunk allowed vlan {format_vlan_list(final['allowed'])}")
        if current - final["allowed"]:
            commands.append(f"no switchport trunk allowed vlan {format_vlan_list(current - final['allowed'])}")

    if final["admin"] == "up" and state["admin"] != "up":
        commands.append("no shutdown")
    return commands


def device_preconditions(commands, state, present_vlans):
    """Reasons the stored device commands are no longer valid for this interface state."""
    parsed = [parse_device_command(c) for c in commands]
    if None in parsed:
        return ["stored commands are not canonical device commands"]
    target_mode = next((value for kind, value in parsed if kind == "mode"), state["mode"])
    current = _current_allowed(state)
    reasons = []
    for kind, value in parsed:
        if kind in ("mode", "access_vlan", "trunk_add", "trunk_remove") and state["mode"] not in ("access", "trunk"):
            reasons.append(f"{state['interface']} is no longer a layer-2 access/trunk port")
            break
        if kind == "access_vlan":
            if target_mode != "access":
                reasons.append(f"{state['interface']} is no longer an access port")
            elif value != state["access_vlan"]:
                reasons += _missing_vlan_reasons([value], present_vlans)
        if kind == "trunk_add":
            added = set(value) - current
            if target_mode != "trunk":
                reasons.append(f"{state['interface']} is no longer a trunk port")
            elif added and (state["access_vlan"] is None or state["access_vlan"] in added):
                reasons.append(f"the untagged VLAN of {state['interface']} is unknown or is in the tagged list")
            reasons += _missing_vlan_reasons(added, present_vlans)
    return reasons


# ---- presentation ---------------------------------------------------------------------------
def _state_summary(state, ctx, safety):
    neighbors = ctx.get("lldp_neighbors")
    return {
        "interface": state["interface"],
        "description": state["description"],
        "admin": state["admin"] or "unknown",
        "mode": state["mode"] or ("routed" if state["routed"] else "unknown"),
        "access_vlan": state["access_vlan"],
        "allowed_vlans": format_vlan_list(state["allowed_vlans"]) if state["mode"] == "trunk" else None,
        "port_channel": state["port_channel"],
        "lldp_neighbor": (", ".join(os10_safety._neighbor_text(n) for n in neighbors) or "none"
                          if neighbors is not None else "not collected"),
        "protected": safety["classification"] == "protected",
        "classification": safety["classification"],
    }


def _requested_summary(state, final):
    requested = {}
    for key in ("description", "admin", "mode", "access_vlan"):
        if final[key] != state[key]:
            requested[key] = final[key]
    if final["mode"] == "trunk" and (final["allowed"] != _current_allowed(state) or state["mode"] != "trunk"):
        requested["allowed_vlans"] = _fmt(final["allowed"])
    return requested


def _change_plan(state, final):
    plan = []
    if final["admin"] != state["admin"]:
        plan.append(f"admin state: {state['admin'] or 'unknown'} → {final['admin']}")
    if final["description"] != state["description"]:
        plan.append(f"description: {state['description'] or 'none'} → {final['description'] or 'removed'}")
    current = _current_allowed(state)
    if final["mode"] != state["mode"]:
        plan.append(f"mode: {state['mode']} → {final['mode']}")
        if final["mode"] == "access" and current:
            plan.append(f"allowed VLANs: {_fmt(current)} → removed (before the mode change)")
        if final["mode"] == "trunk":
            plan.append(f"untagged VLAN: {state['access_vlan']} (kept: OS10 uses the access VLAN as the trunk's "
                        f"untagged VLAN)")
    if final["mode"] == "access" and final["access_vlan"] != state["access_vlan"]:
        plan.append(f"access VLAN: {state['access_vlan']} → {final['access_vlan']}")
    elif final["mode"] == "access" and state["mode"] == "trunk":
        plan.append(f"access VLAN: {state['access_vlan']} (kept: the trunk's untagged VLAN becomes the access VLAN)")
    if final["mode"] == "trunk" and final["allowed"] != current:
        added, removed = final["allowed"] - current, current - final["allowed"]
        detail = ", ".join(part for part in (f"add {_fmt(added)}" if added else "",
                                             f"remove {_fmt(removed)}" if removed else "") if part)
        plan.append(f"allowed VLANs: {_fmt(current)} → {_fmt(final['allowed'])} ({detail})")
    return plan


_MANAGED = ("description ", "shutdown", "no shutdown", "switchport mode ", "switchport access vlan ",
            "switchport trunk allowed vlan ")


def _requested_block(state, final):
    managed, other = [], [line for line in state["block"] if not line.lower().startswith(_MANAGED)]
    if final["description"]:
        managed.append(f"description {final['description']}")
    if final["admin"]:
        managed.append("shutdown" if final["admin"] == "down" else "no shutdown")
    if final["mode"] == "trunk":
        managed.append("switchport mode trunk")
    elif final["mode"] == "access" and any(line.lower() == "switchport mode access" for line in state["block"]):
        managed.append("switchport mode access")
    if final["mode"] in ("access", "trunk") and final["access_vlan"] is not None:
        managed.append(f"switchport access vlan {final['access_vlan']}")
    if final["mode"] == "trunk" and final["allowed"]:
        managed.append(f"switchport trunk allowed vlan {format_vlan_list(final['allowed'])}")
    return managed + other


def _intent_result(intent, state):
    kind, value = intent["kind"], intent["value"]
    current = _current_allowed(state)
    if kind == "description":
        return state["description"] == value, state["description"], "interface_description"
    if kind == "admin":
        return state["admin"] == value, state["admin"], "admin_state"
    if kind == "mode":
        return state["mode"] == value, state["mode"], "switchport_mode"
    if kind == "access_vlan":
        return (state["mode"] == "access" and state["access_vlan"] == value,
                f"{state['mode']}, access VLAN {state['access_vlan']}", "access_vlan")
    operation, vlans = value
    target = set(vlans) if operation == "set" else current | set(vlans) if operation == "add" else current - set(vlans)
    return state["mode"] == "trunk" and target == current, _fmt(current), "trunk_allowed_vlans"


# ---- precheck --------------------------------------------------------------------------------
def plan(intent_lines, config_parents, running_config, ctx):
    """
    intent_lines/config_parents are canonical (changes.policy.check_os10). ctx: see
    changes.os10_safety. Returns a dry-run dict; "rejected": True means nothing may be
    approved (reasons in "rejection_reasons").
    """
    intents = parse_intents(intent_lines)
    state = interface_state(running_config, config_parents[0])
    kinds = change_kinds(intent_lines)
    safety = os10_safety.evaluate(kinds, state, running_config, ctx)
    final, reasons = _simulate(state, intents)
    reasons = safety["reasons"] + reasons

    present = existing_vlans(running_config)
    if not reasons:
        needed = set(final["allowed"]) - _current_allowed(state)
        if final["mode"] == "access" and final["access_vlan"] != state["access_vlan"]:
            needed.add(final["access_vlan"])
        reasons += _missing_vlan_reasons(needed, present)

    commands = [] if reasons else _commands(state, final)
    if commands:
        if any(len(c) > MAX_COMMAND_LENGTH for c in commands):
            reasons.append("the resulting allowed-VLAN list is too long for one command; split the change")
        else:
            try:
                check_os10_commands(commands, config_parents)
            except PolicyViolation as exc:  # planner/grammar mismatch: never send it
                reasons.append(f"internal planning check failed: {exc}")
            reasons += device_preconditions(commands, state, present)
        if reasons:
            commands = []

    results, already_present = [], []
    for intent in intents:
        present_now, current_value, kind = _intent_result(intent, state)
        if present_now:
            already_present.append(intent["line"])
        results.append({"command": intent["line"], "type": kind, "verification_method": VERIFICATION_METHOD,
                        "desired_state_present": present_now, "config_parents": config_parents,
                        "current_value": current_value})

    rejected = bool(reasons)
    return {
        "already_present": already_present,
        "proposed_changes": commands,
        "would_change": bool(commands),
        "verification_method": VERIFICATION_METHOD,
        "command_results": results,
        "device_commands": commands,
        "rejected": rejected,
        "rejection_reasons": reasons,
        "interface": state["interface"],
        "current_state": _state_summary(state, ctx, safety),
        "requested_state": _requested_summary(state, final),
        "safety": {"status": "FAIL" if rejected else "PASS", "classification": safety["classification"],
                   "checks": safety["checks"]},
        "change_plan": [] if rejected else _change_plan(state, final),
        "expected_diff": {
            "current": [config_parents[0]] + [f" {line}" for line in state["block"]],
            "requested": [] if rejected else [config_parents[0]] + [f" {line}" for line in _requested_block(state, final)],
        },
    }
