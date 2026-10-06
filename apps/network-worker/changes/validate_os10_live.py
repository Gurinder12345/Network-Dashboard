"""
READ-ONLY check of the OS10 change path against ONE live Dell OS10 switch, before any write.

Run inside a network-worker pod:
    python -m changes.validate_os10_live Kenda-Core-1 ethernet1/1/<port>

It reads `show running-configuration` (the same read the precheck and backup use) and
`show interface status`, prints ONLY the chosen interface's block and status line, and runs
the V1 policy + verifier for `description AUTOMATION-TEST` as a dry run. It never enters
configuration mode, never creates jobs/approvals/backups, and never prints other config.
"""

import sys

from changes import os10
from changes.policy import check_os10
from db.devices import get_device_by_hostname
from tasks.dell_os10 import get_running_config, run_show_command


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    hostname, interface = sys.argv[1], sys.argv[2]
    device = get_device_by_hostname(hostname)
    if device["platform"] != "dell_os10":
        print(f"STOP: {hostname} is {device['platform']}, not dell_os10")
        return 1

    lines, parents = check_os10(["description AUTOMATION-TEST"], [f"interface {interface}"])
    print(f"policy: OK -> parent={parents[0]!r} lines={lines}")

    result = get_running_config(hostname)[hostname]
    if result["failed"]:
        print(f"STOP: running-configuration read failed: {result['result'][:300]}")
        return 1
    running = result["result"]
    print(f"running-configuration read: {len(running.splitlines())} lines; "
          f"starts with OS10 version header: {running.lstrip().startswith('! Version 10.')}")

    block = os10.interface_block(running, parents[0])
    if block is None:
        print(f"STOP: {parents[0]} not found in running-configuration")
        return 1
    print(f"{parents[0]} block ({len(block)} lines):")
    for line in block:
        print(f"    {line}")

    status = run_show_command(hostname, "show interface status")[hostname]
    short = interface.replace("ethernet", "Eth ")
    matches = [l for l in status["result"].splitlines() if l.strip().startswith((short, interface))]
    print("interface status line:", matches[0] if matches else "(not found - check the table format manually)")

    dry_run = os10.verify(lines, parents, running)
    print(f"dry run: would_change={dry_run['would_change']} current_description="
          f"{dry_run['command_results'][0]['current_value']!r}")
    print("RESULT: parser OK. Nothing was changed on the switch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
