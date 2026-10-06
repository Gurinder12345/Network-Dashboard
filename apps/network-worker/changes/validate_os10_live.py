"""
READ-ONLY dry run of a configuration block against ONE live switch (Dell OS10 or OS6).

Run inside a network-worker pod:
    python -m changes.validate_os10_live Kenda-Core-1 "interface ethernet1/1/<port>" "description AUTOMATION-TEST"
    python -m changes.validate_os10_live Kenda-Core-1 "" "ip routing"          # global block (empty parent)

It reads the running configuration (the same read the precheck uses), prints ONLY the
chosen parent's block (never the rest of the configuration), and shows the CLI preview and
the semantic pre-validation result for each command. It never enters configuration mode
and never creates jobs, approvals, backups or audit rows.
"""

import sys

from changes.blocks import BlockError, normalize_blocks, render_cli
from changes.platforms import platform_for
from changes.verify import precheck_block_issues, verify_blocks
from db.devices import get_device_by_hostname


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    hostname, parent, commands = sys.argv[1], sys.argv[2], sys.argv[3:]
    device = get_device_by_hostname(hostname)
    adapter = platform_for(device["platform"])

    try:
        blocks = normalize_blocks([{"parent": parent or None, "commands": commands}])
    except BlockError as exc:
        print(f"STRUCTURAL VALIDATION: FAIL -> {exc}")
        return 1
    print("STRUCTURAL VALIDATION: PASS")
    print("CLI preview:\n" + "\n".join(f"    {line}" for line in render_cli(blocks).splitlines()))

    try:
        state = adapter.read_device_state(hostname, blocks)
    except Exception as exc:
        print(f"DEVICE CONNECTIVITY: FAIL -> {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    running = state["running_config"]
    print(f"DEVICE CONNECTIVITY: PASS ({adapter.label}; running config {len(running.splitlines())} lines)")

    entry = verify_blocks(adapter.name, blocks, running, state.get("vlan_ids"))[0]
    issues = precheck_block_issues(adapter.name, blocks[0], entry["found"])
    if blocks[0]["parent"]:
        print(f"CURRENT CONFIG CAPTURE: {'found' if entry['found'] else 'not in running configuration'} "
              f"({len(entry['current_config'])} lines)")
        for line in entry["current_config"]:
            print(f"    {line}")
    else:
        print("CURRENT CONFIG CAPTURE: global block (not printed)")
    for result in entry["commands"]:
        print(f"SEMANTIC {result['status']:13} {result['command']}: {result['detail']}")
    print(f"BLOCK PRECHECK: {'FAILED - ' + '; '.join(issues) if issues else 'PASS'}")
    print("RESULT: read-only dry run complete. Nothing was changed on the switch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
