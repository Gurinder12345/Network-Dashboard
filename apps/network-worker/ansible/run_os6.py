import os
import subprocess
import tempfile
import yaml
import json
from vault.client import get_device_credentials
from tasks.dell_os6 import run_show_command

HOSTS_FILE = "/app/inventory/hosts.yaml"
PLAYBOOK = "/app/ansible/playbooks/os6_show_version.yml"


def load_device(target_host):
    with open(HOSTS_FILE, "r") as f:
        inventory = yaml.safe_load(f)

    if target_host not in inventory:
        raise ValueError(
            f"Device not found in inventory: {target_host}"
        )

    device = inventory[target_host]

    if device.get("platform") != "dell_os6":
        raise ValueError(
            f"{target_host} is platform "
            f"{device.get('platform')}, not dell_os6"
        )

    credential_path = device.get(
        "data", {}
    ).get("credential_path")

    if not credential_path:
        raise ValueError(
            f"No credential_path configured for {target_host}"
        )

    return device, credential_path


def run_os6_show_version(target_host):
    device, credential_path = load_device(target_host)

    credentials = get_device_credentials(
        credential_path
    )

    ansible_inventory = {
        "all": {
            "hosts": {
                target_host: {
                    "ansible_host": device["hostname"],
                    "ansible_network_os": "dellemc.os6.os6",
                    "ansible_connection": "ansible.netcommon.network_cli",
                    "ansible_become": True,
                    "ansible_become_method": "enable",
                }
            }
        }
    }

    env = os.environ.copy()

    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    env["ANSIBLE_REMOTE_USER"] = credentials["username"]
    env["ANSIBLE_PASSWORD"] = credentials["password"]
    env["ANSIBLE_BECOME_PASSWORD"] = credentials["secret"]

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yml",
        delete=False,
    ) as temp_inventory:

        yaml.safe_dump(
            ansible_inventory,
            temp_inventory,
            default_flow_style=False,
        )

        inventory_file = temp_inventory.name

    try:
        command = [
            "ansible-playbook",
            "-i",
            inventory_file,
            PLAYBOOK,
        ]

        result = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        return {
            "target_host": target_host,
            "platform": "dell_os6",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    finally:
        if os.path.exists(inventory_file):
            os.remove(inventory_file)



def normalize_config_line(line):
    normalized = line.strip()

    if normalized.startswith("description "):
        value = normalized[len("description "):].strip()

        if (
            len(value) >= 2
            and value.startswith('"')
            and value.endswith('"')
        ):
            value = value[1:-1]

        normalized = f"description {value}"

    return normalized


def get_show_output(target_host, command):
    result = run_show_command(
        target_host,
        command,
    )

    device_result = result[target_host]

    if device_result["failed"]:
        raise RuntimeError(
            device_result["result"]
        )

    return device_result["result"]


def extract_parent_config(
    running_config,
    config_parents=None,
):
    config_parents = config_parents or []

    if not config_parents:
        return running_config

    scoped_config = running_config

    for parent in config_parents:
        parent = parent.strip()
        lines = scoped_config.splitlines()

        start_index = None

        for index, line in enumerate(lines):
            if line.strip() == parent:
                start_index = index + 1
                break

        if start_index is None:
            return ""

        block = []

        for line in lines[start_index:]:
            if line.strip() == "exit":
                break

            block.append(line)

        scoped_config = "\n".join(block)

    return scoped_config


def classify_config_command(command):
    normalized = command.strip().lower()
    parts = normalized.split()

    # no vlan 500
    if (
        len(parts) == 3
        and parts[0] == "no"
        and parts[1] == "vlan"
        and parts[2].isdigit()
    ):
        return "vlan_negative"

    # vlan 500
    if (
        len(parts) == 2
        and parts[0] == "vlan"
        and parts[1].isdigit()
    ):
        return "vlan"

    # no description, no shutdown, no ip address, etc.
    if normalized.startswith("no "):
        return "negative"

    # default
    return "running_config"


def parse_vlan_ids(vlan_output):
    vlan_ids = set()

    for line in vlan_output.splitlines():
        stripped = line.strip()

        if not stripped:
            continue

        fields = stripped.split()

        if fields and fields[0].isdigit():
            vlan_ids.add(int(fields[0]))

    return vlan_ids


def verify_config_commands(
    target_host,
    config_lines,
    config_parents=None,
):
    command_types = [
        classify_config_command(command)
        for command in config_lines
    ]

    needs_vlan = any(
        command_type in ("vlan", "vlan_negative")
        for command_type in command_types
    )

    needs_running_config = any(
        command_type in ("running_config", "negative")
        for command_type in command_types
    )

    vlan_ids = set()
    existing_lines = set()

    if needs_vlan:
        vlan_output = get_show_output(
            target_host,
            "show vlan",
        )

        vlan_ids = parse_vlan_ids(
            vlan_output
        )

    if needs_running_config:
        running_config = get_show_output(
            target_host,
            "show running-config",
        )

        scoped_config = extract_parent_config(
            running_config,
            config_parents,
        )

        existing_lines = {
            normalize_config_line(line)
            for line in scoped_config.splitlines()
            if line.strip()
        }

    already_present = []
    proposed_changes = []
    command_results = []
    verification_methods = set()

    for command in config_lines:
        normalized = normalize_config_line(command)

        command_type = classify_config_command(
            normalized
        )

        if command_type == "vlan":
            vlan_id = int(
                normalized.split()[1]
            )

            desired_state_present = (
                vlan_id in vlan_ids
            )

            verification_method = "show vlan"

        elif command_type == "vlan_negative":
            vlan_id = int(
                normalized.split()[2]
            )

            desired_state_present = (
                vlan_id not in vlan_ids
            )

            verification_method = "show vlan"

        elif command_type == "negative":
            positive_form = normalized[3:].strip()

            positive_exists = any(
                existing == positive_form
                or existing.startswith(
                    f"{positive_form} "
                )
                for existing in existing_lines
            )

            desired_state_present = (
                not positive_exists
            )

            verification_method = "running-config"

        else:
            desired_state_present = (
                normalized in existing_lines
            )

            verification_method = "running-config"

        verification_methods.add(
            verification_method
        )

        if desired_state_present:
            already_present.append(normalized)
        else:
            proposed_changes.append(normalized)

        command_results.append({
            "command": normalized,
            "type": command_type,
            "verification_method": verification_method,
            "desired_state_present": desired_state_present,
            "config_parents": config_parents or [],
        })

    if len(verification_methods) == 1:
        overall_method = next(
            iter(verification_methods)
        )
    else:
        overall_method = "mixed"

    return {
        "already_present": already_present,
        "proposed_changes": proposed_changes,
        "would_change": bool(proposed_changes),
        "verification_method": overall_method,
        "command_results": command_results,
    }


def run_os6_config_check(
    target_host,
    config_lines,
    config_parents=None,
):
    load_device(target_host)

    if not isinstance(config_lines, list):
        raise ValueError(
            "config_lines must be a list"
        )

    if not config_lines:
        raise ValueError(
            "config_lines cannot be empty"
        )

    config_parents = config_parents or []

    if not isinstance(config_parents, list):
        raise ValueError(
            "config_parents must be a list"
        )

    comparison = verify_config_commands(
        target_host,
        config_lines,
        config_parents,
    )

    return {
        "target_host": target_host,
        "platform": "dell_os6",
        "dry_run": True,
        **comparison,
    }

def run_os6_config_apply(
    target_host,
    config_lines,
    config_parents=None,
):
    


    config_parents = config_parents or []

    if not isinstance(config_parents, list):
        raise ValueError("config_parents must be a list")

    if not isinstance(config_lines, list):
        raise ValueError("config_lines must be a list")

    if not config_lines:
        raise ValueError("config_lines cannot be empty")







    device, credential_path = load_device(target_host)

    

    credentials = get_device_credentials(
        credential_path
    )

    ansible_inventory = {
        "all": {
            "hosts": {
                target_host: {
                    "ansible_host": device["hostname"],
                    "ansible_network_os": "dellemc.os6.os6",
                    "ansible_connection": "ansible.netcommon.network_cli",
                    "ansible_become": True,
                    "ansible_become_method": "enable",
                }
            }
        }
    }

    env = os.environ.copy()

    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    env["ANSIBLE_REMOTE_USER"] = credentials["username"]
    env["ANSIBLE_PASSWORD"] = credentials["password"]
    env["ANSIBLE_BECOME_PASSWORD"] = credentials["secret"]

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yml",
        delete=False,
    ) as temp_inventory:

        yaml.safe_dump(
            ansible_inventory,
            temp_inventory,
        )

        inventory_file = temp_inventory.name

    try:
        command = [
            "ansible-playbook",
            "-i",
            inventory_file,
            "/app/ansible/playbooks/os6_config_apply.yml",
            "--extra-vars",
            json.dumps({
                "config_lines": config_lines,
                "config_parents": config_parents,
            }),
        ]

        result = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

        return {
            "target_host": target_host,
            "platform": "dell_os6",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    finally:
        if os.path.exists(inventory_file):
            os.remove(inventory_file)
