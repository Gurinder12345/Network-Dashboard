import os
import subprocess

from vault.client import get_device_credentials


def run_os6_show_version(target_host):
    credentials = get_device_credentials(
        f"network/devices/{target_host}"
    )

    env = os.environ.copy()

    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    env["ANSIBLE_REMOTE_USER"] = credentials["username"]
    env["ANSIBLE_PASSWORD"] = credentials["password"]
    env["ANSIBLE_BECOME_PASSWORD"] = credentials["secret"]

    command = [
        "ansible-playbook",
        "-i",
        "/app/ansible/inventory.yml",
        "/app/ansible/playbooks/os6_show_version.yml",
        "--limit",
        target_host,
    ]

    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
