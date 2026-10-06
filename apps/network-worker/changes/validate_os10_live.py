"""
READ-ONLY check of the OS10 change path against ONE live Dell OS10 switch, before any write.

Run inside a network-worker pod:
    python -m changes.validate_os10_live Kenda-Core-1 ethernet1/1/<port>
    python -m changes.validate_os10_live Kenda-Core-1 ethernet1/1/<port> "switchport access vlan 20"
    python -m changes.validate_os10_live Kenda-Core-1 ethernet1/1/<port> "switchport mode trunk" \
        "switchport trunk allowed vlan 30"

It reads `show running-configuration` (+ `show lldp neighbors` for L2/admin changes, in the
same session: the same reads the precheck uses), prints ONLY the chosen interface's block,
its parsed state, its LLDP neighbors and VLAN existence, and runs the policy + safety
classification + planner as a dry run (default request: `description AUTOMATION-TEST`).
It never enters configuration mode, never creates jobs/approvals/backups/audit rows, and
never prints other configuration.
"""

import sys

from changes import os10
from changes.platforms import platform_for
from changes.policy import PolicyViolation
from db.devices import get_device_by_hostname


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    hostname, interface, requested = sys.argv[1], sys.argv[2], sys.argv[3:] or ["description AUTOMATION-TEST"]
    device = get_device_by_hostname(hostname)
    if device["platform"] != "dell_os10":
        print(f"STOP: {hostname} is {device['platform']}, not dell_os10")
        return 1
    adapter = platform_for("dell_os10")

    try:
        lines, parents = adapter.prepare(requested, [f"interface {interface}"])
    except PolicyViolation as exc:
        print(f"policy: REJECTED -> {exc}")
        return 1
    print(f"policy: OK -> parent={parents[0]!r} lines={lines}")

    try:
        state = adapter.read_device_state(hostname, lines)
    except Exception as exc:
        print(f"STOP: device read failed: {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    running = state["running_config"]
    print(f"running-configuration read: {len(running.splitlines())} lines; "
          f"starts with OS10 version header: {running.lstrip().startswith('! Version 10.')}")
    print(f"show lldp neighbors: {'read' if state.get('lldp') is not None else state.get('lldp_error')}")

    block = os10.interface_block(running, parents[0])
    if block is None:
        print(f"STOP: {parents[0]} not found in running-configuration")
        return 1
    print(f"{parents[0]} block ({len(block)} lines):")
    for line in block:
        print(f"    {line}")

    parsed = os10.parse_interface(block, parents[0].split()[1])
    print("parsed state:", {k: parsed[k] for k in ("description", "admin", "mode", "access_vlan", "allowed_vlans",
                                                  "port_channel", "routed", "admin_issues", "switchport_issues")})
    print("management:", os10.management_location(running, device["management_ip"])["detail"])
    print("VLANs present (interface vlanN):", sorted(os10.existing_vlans(running)))

    dry_run = adapter.plan(hostname, device, lines, parents, state)
    print("current state:", dry_run["current_state"])
    print("safety:", dry_run["safety"]["status"], "/", dry_run["safety"]["classification"])
    for check in dry_run["safety"]["checks"]:
        print(f"    {check['status']:8} {check['name']}: {check['detail']}")
    if dry_run["rejected"]:
        print("REJECTED:")
        for reason in dry_run["rejection_reasons"]:
            print(f"    {reason}")
    else:
        print("change plan:", dry_run["change_plan"] or "no change required")
        print("device commands:", dry_run["device_commands"])
    print("RESULT: read-only dry run complete. Nothing was changed on the switch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
