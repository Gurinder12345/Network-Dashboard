"""
Dell OS10 running-configuration verification for the V1 command set (interface
description). Used by precheck (is a change needed?), pre-apply (still needed?) and
post-check (did the device take it?). Output has the same shape as the OS6 verifier
(ansible/run_os6.py verify_config_commands), so the shared workflow and the UI treat
both platforms identically.

OS10 running-configuration lists each interface as a block:

    interface ethernet1/1/5
     description AUTOMATION-TEST
     no shutdown
     switchport access vlan 1
    !

The block starts at the exact `interface ethernetX/Y/Z` line and ends at the next `!`
or the next non-indented line. A missing interface is an error, never "no change".
"""


class VerificationError(RuntimeError):
    pass


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


def _description(block):
    for line in block:
        if line.lower().startswith("description "):
            value = line[len("description "):].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            return value
    return None


def verify(config_lines, config_parents, running_config):
    """config_lines/parents are already canonical (changes.policy.check_os10)."""
    if not running_config or "interface ethernet" not in running_config:
        raise VerificationError("Running configuration was empty or not recognisable as Dell OS10 output")

    parent = config_parents[0]
    block = interface_block(running_config, parent)
    if block is None:
        raise VerificationError(f"{parent} was not found in the running configuration")

    current = _description(block)
    already_present, proposed, results = [], [], []
    for command in config_lines:
        if command == "no description":
            present = current is None
        else:
            present = current == command[len("description "):]
        (already_present if present else proposed).append(command)
        results.append({
            "command": command,
            "type": "interface_description",
            "verification_method": "running-configuration",
            "desired_state_present": present,
            "config_parents": config_parents,
            "current_value": current,
        })

    return {
        "already_present": already_present,
        "proposed_changes": proposed,
        "would_change": bool(proposed),
        "verification_method": "running-configuration",
        "command_results": results,
    }
