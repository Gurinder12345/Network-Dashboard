"""
Dell OS10 interface safety classification for safe-L2 changes. Pure: the platform adapter
gathers the data (running-configuration, live LLDP, stored topology, protected list) and
passes it in a context dict; nothing here contacts a device or the database.

Which checks a change needs depends on what it does:

    description / no description : explicit protection only (unchanged V1 behaviour)
    switchport changes           : protection, routed port, port-channel member, VLT,
                                   management path, ANY LLDP neighbor (live or stored),
                                   switchport state known
    shutdown                     : the same as switchport, plus admin state known
    no shutdown                  : protection, routed port, port-channel member, VLT,
                                   management path, no NETWORK-DEVICE neighbor (live or
                                   stored), admin state known

Fail closed: a check whose data is missing or ambiguous is "unknown", and unknown rejects
the change ("Unable to establish that this interface is safe for automated modification").
The summary `show lldp neighbors` output carries no capabilities, so an LLDP neighbor that
does not resolve to an inventory device cannot be proven to be an endpoint: any LLDP
neighbor blocks switchport and shutdown changes. Nothing is inferred from interface
numbers or descriptions.
"""

from changes.os10 import management_location, vlt_interfaces
from changes.vlans import format_vlan_list

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"


class SafetyRejection(RuntimeError):
    """The interface is not (or no longer) safe for the change. Message is user-facing."""


UNSAFE = "Unable to establish that this interface is safe for automated modification."

REQUIRES = {
    "description": ("protected",),
    "switchport": ("protected", "routed", "port_channel", "vlt", "management", "lldp", "topology", "switchport_state"),
    "shutdown": ("protected", "routed", "port_channel", "vlt", "management", "lldp", "topology", "admin_state"),
    "no_shutdown": ("protected", "routed", "port_channel", "vlt", "management", "lldp_infra", "topology_infra",
                    "admin_state"),
}
CHECK_ORDER = ("protected", "management", "routed", "port_channel", "vlt", "lldp", "lldp_infra", "topology",
               "topology_infra", "switchport_state", "admin_state")
CHECK_NAMES = {"protected": "explicit protection", "management": "management path", "routed": "routed port",
               "port_channel": "port-channel membership", "vlt": "VLT interconnect", "lldp": "LLDP neighbor (live)",
               "lldp_infra": "network-device neighbor (live LLDP)", "topology": "LLDP neighbor (stored topology)",
               "topology_infra": "network-device neighbor (stored topology)", "switchport_state": "switchport state",
               "admin_state": "admin state"}
KIND_LABELS = {"shutdown": "shutdown", "no_shutdown": "no shutdown", "switchport": "switchport change",
               "description": "description change"}


def _neighbor_text(n):
    name = n.get("remote_system_name") or n.get("remote_chassis_id") or "unnamed neighbor"
    port = n.get("remote_interface") or n.get("remote_port_id")
    text = f"{name} {port}" if port else name
    if n.get("remote_hostname"):
        text += f" = inventory device {n['remote_hostname']}"
    return text


def _neighbors_check(neighbors, error, infra_only, source):
    if neighbors is None:
        return UNKNOWN, f"{source} not available: {error or 'not collected'}", None
    relevant = [n for n in neighbors if n.get("remote_device_id") or not infra_only]
    if not relevant:
        return PASS, f"no {'network-device ' if infra_only else ''}neighbor ({source})", None
    infra = [n for n in relevant if n.get("remote_device_id")]
    detail = f"{source}: " + ", ".join(_neighbor_text(n) for n in relevant[:3])
    if infra:
        return FAIL, detail, "an uplink"
    return FAIL, detail + "; cannot confirm the neighbor is not network infrastructure", "an LLDP-connected port"


def _check(name, state, running_config, ctx):
    """(status, detail, classification-if-failed)."""
    interface = state["interface"]
    if name == "protected":
        if ctx.get("protected_error"):
            return UNKNOWN, f"protected-interface list is invalid: {ctx['protected_error']}", None
        if interface in (ctx.get("protected") or set()):
            return FAIL, "listed in OS10_PROTECTED_INTERFACES", "protected"
        return PASS, "not in the protected-interface list", None

    if name == "routed":
        if state["routed"]:
            return FAIL, "routed port (no switchport / IP address configured)", "a routed port"
        return PASS, "layer-2 port", None

    if name == "port_channel":
        if state["port_channel"]:
            return FAIL, f"member of port-channel {state['port_channel']}", "a port-channel member"
        return PASS, "not a port-channel member", None

    if name == "vlt":
        members, error = vlt_interfaces(running_config)
        if error:
            return UNKNOWN, error, None
        if interface in members:
            return FAIL, "VLT discovery (VLTi) interface", "a VLT interconnect"
        return PASS, "not a VLT interconnect", None

    if name == "management":
        where = management_location(running_config, ctx.get("management_ip"))
        if where["kind"] == "out_of_band":
            return PASS, where["detail"], None
        if where["kind"] == "interface":
            if where["interface"] == interface:
                return FAIL, where["detail"], "the management interface"
            return PASS, where["detail"], None
        if where["kind"] == "vlan":
            vlan = where["vlan"]
            if state["access_vlan"] == vlan or vlan in state["allowed_vlans"]:
                return FAIL, f"{where['detail']} and this port carries VLAN {vlan}", "on the management path"
            if state["mode"] is None:
                return UNKNOWN, f"{where['detail']}; this port's VLANs cannot be determined", None
            return PASS, f"{where['detail']}; this port does not carry it", None
        return UNKNOWN, where["detail"], None

    if name in ("lldp", "lldp_infra"):
        return _neighbors_check(ctx.get("lldp_neighbors"), ctx.get("lldp_error"), name == "lldp_infra", "live LLDP")

    if name in ("topology", "topology_infra"):
        return _neighbors_check(ctx.get("stored_links"), ctx.get("stored_error"), name == "topology_infra",
                                "stored topology")

    if name == "switchport_state":
        if state["mode"] in ("access", "trunk"):
            vlans = (f"access VLAN {state['access_vlan']}" if state["mode"] == "access"
                     else f"allowed {format_vlan_list(state['allowed_vlans']) or 'none'}")
            return PASS, f"{state['mode']} mode, {vlans}", None
        if state["mode"] == "routed":
            return FAIL, "routed port", "a routed port"
        return UNKNOWN, "; ".join(state["switchport_issues"]) or "switchport mode not determined", None

    if name == "admin_state":
        if state["admin"]:
            return PASS, f"admin {state['admin']}", None
        return UNKNOWN, "; ".join(state["admin_issues"]), None

    raise ValueError(name)


def evaluate(kinds, state, running_config, ctx):
    """
    kinds: set of change kinds (changes.policy.change_kinds). Returns
    {"status": PASS|FAIL, "classification", "checks": [...], "reasons": [...]}.
    """
    needed = {name for kind in kinds for name in REQUIRES[kind]}
    results = {}
    for name in CHECK_ORDER:
        if name in needed:
            results[name] = _check(name, state, running_config, ctx)

    checks = [{"name": CHECK_NAMES[name], "status": status, "detail": detail}
              for name, (status, detail, _) in results.items()]

    reasons = []
    for kind in ("shutdown", "no_shutdown", "switchport", "description"):
        if kind not in kinds:
            continue
        for name in REQUIRES[kind]:
            status, detail, classification = results[name]
            if status == FAIL:
                reasons.append(f"{KIND_LABELS[kind]} rejected: {state['interface']} is classified as "
                               f"{classification} ({detail}).")
                break
            if status == UNKNOWN:
                reasons.append(f"{KIND_LABELS[kind]} rejected: {UNSAFE} ({CHECK_NAMES[name]}: {detail})")
                break

    failed = [results[name][2] for name in CHECK_ORDER if name in results and results[name][0] == FAIL]
    unknown = any(r[0] == UNKNOWN for r in results.values())
    classification = failed[0] if failed else ("unknown" if unknown else "eligible")
    return {"status": "FAIL" if reasons else "PASS", "classification": classification, "checks": checks,
            "reasons": reasons}
