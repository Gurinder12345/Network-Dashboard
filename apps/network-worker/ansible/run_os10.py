"""
Dell OS10 configuration apply through Ansible (writes only; reads use Netmiko/Nornir).

Mirrors ansible/run_os6.py: credentials from Vault are passed to ansible-playbook as
environment variables (never written to the temporary inventory or logged), the
playbook enters/exits configuration mode itself, and the caller decides success from
the post-check, not from Ansible's return code alone.
"""

import json
import os
import subprocess
import tempfile

import yaml

from vault.client import get_device_credentials

HOSTS_FILE = os.getenv("NETWORK_HOSTS_FILE", "/app/inventory/hosts.yaml")
PLAYBOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbooks", "os10_config_apply.yml")
APPLY_TIMEOUT_SECONDS = int(os.getenv("OS10_APPLY_TIMEOUT_SECONDS", "180"))


def load_device(target_host, hosts_file=None):
    with open(hosts_file or HOSTS_FILE, "r") as handle:
        inventory = yaml.safe_load(handle)

    device = inventory.get(target_host)
    if device is None:
        raise ValueError(f"Device not found in inventory: {target_host}")
    if device.get("platform") != "dell_os10":
        raise ValueError(f"{target_host} is platform {device.get('platform')}, not dell_os10")

    credential_path = device.get("data", {}).get("credential_path")
    if not credential_path:
        raise ValueError(f"No credential_path configured for {target_host}")
    return device, credential_path


def run_os10_config_apply(target_host, config_lines, config_parents=None):
    config_parents = config_parents or []
    if not isinstance(config_lines, list) or not config_lines:
        raise ValueError("config_lines must be a non-empty list")
    if not isinstance(config_parents, list):
        raise ValueError("config_parents must be a list")

    device, credential_path = load_device(target_host)
    credentials = get_device_credentials(credential_path)

    host_vars = {
        "ansible_host": device["hostname"],
        "ansible_port": int(device.get("port") or 22),
        "ansible_network_os": "dellemc.os10.os10",
        "ansible_connection": "ansible.netcommon.network_cli",
        # OS10 admin users land in privileged EXEC (#); there is no enable step.
        "ansible_become": False,
    }
    inventory = {"all": {"hosts": {target_host: host_vars}}}

    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update({
        "ANSIBLE_HOST_KEY_CHECKING": "False",
        "ANSIBLE_REMOTE_USER": credentials["username"],
        "ANSIBLE_PASSWORD": credentials["password"],
        "ANSIBLE_PERSISTENT_CONNECT_TIMEOUT": "30",
        "ANSIBLE_PERSISTENT_COMMAND_TIMEOUT": "60",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
    })

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as temp_inventory:
        yaml.safe_dump(inventory, temp_inventory)
        inventory_file = temp_inventory.name

    try:
        command = [
            "ansible-playbook",
            "-i", inventory_file,
            PLAYBOOK,
            "--extra-vars", json.dumps({"config_lines": config_lines, "config_parents": config_parents}),
        ]
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=APPLY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return {"target_host": target_host, "platform": "dell_os10", "returncode": -1, "stdout": "",
                    "stderr": f"ansible-playbook timed out after {APPLY_TIMEOUT_SECONDS} s"}

        return {
            "target_host": target_host,
            "platform": "dell_os10",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    finally:
        if os.path.exists(inventory_file):
            os.remove(inventory_file)


def run_os10_blocks_apply(target_host, steps):
    """Ordered multi-block apply (ansible/playbooks/config_blocks_apply.yml)."""
    from ansible.blocks_runner import run_blocks_playbook

    device, credential_path = load_device(target_host)
    credentials = get_device_credentials(credential_path)
    host_vars = {
        "ansible_host": device["hostname"],
        "ansible_port": int(device.get("port") or 22),
        "ansible_network_os": "dellemc.os10.os10",
        "ansible_connection": "ansible.netcommon.network_cli",
        # OS10 admin users land in privileged EXEC (#); there is no enable step.
        "ansible_become": False,
    }
    return run_blocks_playbook(target_host, "dell_os10", host_vars, credentials, steps)
