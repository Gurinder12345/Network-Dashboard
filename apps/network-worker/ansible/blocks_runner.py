"""
Run config_blocks_apply.yml for one device and report exactly which steps the device
accepted. Shared by Dell OS6 (run_os6.py) and Dell OS10 (run_os10.py); only the
connection variables differ.

Credentials are passed to ansible-playbook as environment variables (never written to the
temporary inventory, the command line or any log). The returned stdout/stderr never
contain them: the playbook reads them with lookup('env') and Ansible does not print vars.
"""

import base64
import json
import os
import re
import subprocess
import tempfile

import yaml

PLAYBOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbooks", "config_blocks_apply.yml")
MARKER = re.compile(r"NMP_APPLY_RESULT:([A-Za-z0-9+/=]+)")
BASE_TIMEOUT_SECONDS = int(os.getenv("CHANGE_APPLY_BASE_TIMEOUT_SECONDS", "180"))
PER_STEP_SECONDS = float(os.getenv("CHANGE_APPLY_PER_STEP_SECONDS", "3"))
MAX_TIMEOUT_SECONDS = int(os.getenv("CHANGE_APPLY_MAX_TIMEOUT_SECONDS", "3600"))


def parse_marker(stdout):
    """{'applied': [step indexes], 'failed': index|None, 'error': str} or None if absent/corrupt."""
    match = MARKER.search(stdout or "")
    if not match:
        return None
    try:
        data = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
        applied = [int(i) for i in data.get("applied") or []]
        failed = data.get("failed")
        return {"applied": applied, "failed": None if failed in (None, "", "None") else int(failed),
                "error": str(data.get("error") or "")}
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def run_blocks_playbook(target_host, platform, host_vars, credentials, steps):
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list")

    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update({
        "ANSIBLE_HOST_KEY_CHECKING": "False",
        "ANSIBLE_REMOTE_USER": credentials["username"],
        "ANSIBLE_PASSWORD": credentials["password"],
        "ANSIBLE_BECOME_PASSWORD": credentials.get("secret") or "",
        "ANSIBLE_PERSISTENT_CONNECT_TIMEOUT": "30",
        "ANSIBLE_PERSISTENT_COMMAND_TIMEOUT": "60",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
    })
    timeout = min(MAX_TIMEOUT_SECONDS, int(BASE_TIMEOUT_SECONDS + PER_STEP_SECONDS * len(steps)))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as inventory:
        yaml.safe_dump({"all": {"hosts": {target_host: host_vars}}}, inventory)
        inventory_file = inventory.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as extra:
        json.dump({"apply_steps": [{"i": s["i"], "block": s["block"], "kind": s["kind"], "command": s["command"]}
                                   for s in steps]}, extra)
        extra_file = extra.name

    try:
        command = ["ansible-playbook", "-i", inventory_file, PLAYBOOK, "--extra-vars", f"@{extra_file}"]
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"target_host": target_host, "platform": platform, "returncode": -1, "stdout": "",
                    "stderr": f"ansible-playbook timed out after {timeout} s", "progress": None}
        return {"target_host": target_host, "platform": platform, "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr, "progress": parse_marker(result.stdout)}
    finally:
        for path in (inventory_file, extra_file):
            if os.path.exists(path):
                os.remove(path)
