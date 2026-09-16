import os
import subprocess
import tempfile
import yaml
import json
from vault.client import get_device_credentials


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



def run_os6_config_check(target_host, config_lines):
    device, credential_path = load_device(target_host)

    if not isinstance(config_lines, list):
        raise ValueError("config_lines must be a list")

    if not config_lines:
        raise ValueError("config_lines cannot be empty")

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
            "/app/ansible/playbooks/os6_config_check.yml",
            "--extra-vars",
            json.dumps({
                "config_lines": config_lines
            }),
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
            "dry_run": True,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    finally:
        if os.path.exists(inventory_file):
            os.remove(inventory_file)


def compare_config_lines(running_config, config_lines):
    existing_lines = {
        line.strip()
        for line in running_config.splitlines()
        if line.strip()
    }

    already_present = []
    proposed = []

    for line in config_lines:
        normalized = line.strip()

        if normalized in existing_lines:
            already_present.append(normalized)
        else:
            proposed.append(normalized)

    return {
        "already_present": already_present,
        "proposed_changes": proposed,
        "would_change": len(proposed) > 0,
    }
