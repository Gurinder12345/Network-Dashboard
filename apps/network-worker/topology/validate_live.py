"""
Manual, read-only LLDP parser validation against ONE live switch.

Run inside the network-worker pod:
    python -m topology.validate_live Kenda-Core-1 --expect-platform dell_os10
    python -m topology.validate_live Kenda-HARO-IDF-A --expect-platform dell_os6

What it does: reads the device from inventory, gets credentials via the existing Vault
flow, runs exactly one LLDP show command (chosen by platform), saves the raw output to
/tmp/lldp_real_<os10|os6>.txt (mode 0600), parses it and prints the result.

What it never does: write topology rows, touch Redis, create jobs/approvals/backups/audit
events, or send any configuration command. Credentials are never printed.

Exit code: 0 = parsed with no warnings; 1 = stop condition (see output).
"""

import argparse
import json
import os
import sys

from db.devices import get_device_by_hostname
from topology.discovery import collect_lldp_output
from topology.lldp import LLDP_COMMANDS, count_table_rows, parse_lldp_output


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hostname")
    parser.add_argument("--expect-platform", choices=sorted(LLDP_COMMANDS), required=True)
    args = parser.parse_args()

    device = get_device_by_hostname(args.hostname)  # read-only; enabled devices only
    stop = []

    if device["platform"] != args.expect_platform:
        print(f"STOP: {args.hostname} is {device['platform']}, expected {args.expect_platform}")
        return 1

    command, output = collect_lldp_output(device)
    expected_command = LLDP_COMMANDS[args.expect_platform]
    if command != expected_command:
        stop.append(f"command was {command!r}, expected {expected_command!r}")

    suffix = "os10" if device["platform"] == "dell_os10" else "os6"
    path = f"/tmp/lldp_real_{suffix}.txt"
    with open(path, "w") as handle:
        handle.write(output)
    os.chmod(path, 0o600)

    print(f"DEVICE:  {device['hostname']} ({device['platform']})")
    print(f"COMMAND: {command}")
    print(f"RAW OUTPUT SAVED: {path} ({len(output.splitlines())} lines)")

    try:
        neighbors, warnings = parse_lldp_output(device["platform"], output)
    except Exception as exc:
        print(f"STOP: parser raised {type(exc).__name__}: {exc}")
        return 1

    raw_rows = count_table_rows(output)
    print(f"\nPARSED NEIGHBORS ({len(neighbors)}):")
    print(json.dumps(neighbors, indent=1))
    print(f"\nWARNINGS ({len(warnings)}):")
    for warning in warnings:
        print(f"  - {warning}")

    if warnings:
        stop.append("parser warnings present")
    if raw_rows is not None and raw_rows != len(neighbors):
        stop.append(f"table has {raw_rows} data rows but {len(neighbors)} neighbors were parsed "
                    "(wrapped/continuation lines or a missed row - inspect the raw output)")
    if raw_rows is None:
        stop.append("no dashed underline row found - output format differs from fixture assumptions")
    if any(not n["local_interface"] for n in neighbors):
        stop.append("a neighbor has no local interface")

    print("\nRESULT:", "STOP - " + "; ".join(stop) if stop else "PARSED CLEANLY - now compare every row with the CLI by eye")
    return 1 if stop else 0


if __name__ == "__main__":
    sys.exit(main())
