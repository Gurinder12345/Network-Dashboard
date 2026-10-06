from nornir import InitNornir
from nornir.core.filter import F
from nornir.core.inventory import ConnectionOptions
from nornir_netmiko.tasks import netmiko_send_command

from vault.client import get_device_credentials
from datetime import datetime, timezone
import hashlib
import os

# Nornir inventory directory (overridable so tests can target a simulated device).
INVENTORY_DIR = os.getenv("NORNIR_INVENTORY_DIR", "/app/inventory")

def get_nornir(target_host):
    nr = InitNornir(
        inventory={
            "plugin": "SimpleInventory",
            "options": {
                "host_file": os.path.join(INVENTORY_DIR, "hosts.yaml"),
                "group_file": os.path.join(INVENTORY_DIR, "groups.yaml"),
                "defaults_file": os.path.join(INVENTORY_DIR, "defaults.yaml"),
            },
        }
    )

    nr = nr.filter(F(name=target_host))

    if len(nr.inventory.hosts) == 0:
        raise ValueError(
            f"Device not found in inventory: {target_host}"
        )

    for host in nr.inventory.hosts.values():

        if host.platform != "dell_os10":
            raise ValueError(
                f"{host.name} is platform {host.platform}, not dell_os10"
            )

        credential_path = host.data["credential_path"]
        credentials = get_device_credentials(credential_path)

        host.username = credentials["username"]
        host.password = credentials["password"]

        host.connection_options["netmiko"] = ConnectionOptions(
            extras={
                "secret": credentials.get("secret", "")
            }
        )

    return nr


def run_show_command(target_host, command):
    nr = get_nornir(target_host)

    result = nr.run(
        task=netmiko_send_command,
        command_string=command,
    )

    output = {}

    for hostname, multi_result in result.items():
        task_result = multi_result[0]

        output[hostname] = {
            "failed": task_result.failed,
            "result": str(task_result.result),
        }

    return output



def _failed_prep(prep):
    """
    If the first step (login + `terminal length 0`) failed, return a per-host failure with
    the real exception. Otherwise Nornir would silently skip the failed host on the next
    run and the caller would get an empty result instead of e.g. an authentication error.
    """
    if not prep.failed:
        return None
    output = {}
    for hostname, multi_result in prep.items():
        task_result = multi_result[0]
        exc = task_result.exception
        output[hostname] = {
            "failed": True,
            "result": f"{type(exc).__name__}: {exc}" if exc else str(task_result.result),
        }
    return output


def get_running_config(target_host):
    nr = get_nornir(target_host)

    # Disable paging first
    failed = _failed_prep(nr.run(
        task=netmiko_send_command,
        command_string="terminal length 0",
        read_timeout=30,
    ))
    if failed:
        return failed

    result = nr.run(
        task=netmiko_send_command,
        command_string="show running-configuration",
        use_timing=True,
        read_timeout=120,
    )

    output = {}

    for hostname, multi_result in result.items():
        task_result = multi_result[0]

        output[hostname] = {
            "failed": task_result.failed,
            "result": str(task_result.result),
        }

    return output



def get_running_config_and_lldp(target_host):
    """
    Read-only, ONE SSH session: `show running-configuration` then `show lldp neighbors`
    (safe-L2 change precheck / pre-apply). An LLDP failure does not fail the read; it is
    reported as lldp_error and the caller treats the neighbor state as unknown.
    """
    nr = get_nornir(target_host)
    try:
        failed = _failed_prep(nr.run(
            task=netmiko_send_command,
            command_string="terminal length 0",
            read_timeout=30,
        ))
        if failed:
            return failed

        config = nr.run(
            task=netmiko_send_command,
            command_string="show running-configuration",
            use_timing=True,
            read_timeout=120,
        )
        lldp = nr.run(
            task=netmiko_send_command,
            command_string="show lldp neighbors",
            read_timeout=60,
        )

        output = {}
        for hostname, multi_result in config.items():
            task_result = multi_result[0]
            if task_result.failed:
                output[hostname] = {"failed": True, "result": str(task_result.result)}
                continue
            entry = {"failed": False, "running_config": str(task_result.result), "lldp": None, "lldp_error": None}
            lldp_result = lldp.get(hostname)
            if lldp_result is None or lldp_result[0].failed:
                entry["lldp_error"] = "show lldp neighbors failed"
            else:
                entry["lldp"] = str(lldp_result[0].result)
            output[hostname] = entry
        return output
    finally:
        nr.close_connections()


def run_show_commands(target_host, commands):
    """Read-only operator verification commands, one SSH session. Returns [{command, failed, output}]."""
    nr = get_nornir(target_host)
    try:
        failed = _failed_prep(nr.run(task=netmiko_send_command, command_string="terminal length 0", read_timeout=30))
        if failed:
            return [{"command": c, "failed": True, "output": failed[target_host]["result"]} for c in commands]
        results = []
        for command in commands:
            run = nr.run(task=netmiko_send_command, command_string=command, read_timeout=120)
            task_result = run[target_host][0] if target_host in run else None
            results.append({"command": command, "failed": task_result is None or task_result.failed,
                            "output": str(task_result.result) if task_result is not None else "not run"})
        return results
    finally:
        nr.close_connections()


def backup_running_config(target_host):
    nr = get_nornir(target_host)

    failed = _failed_prep(nr.run(
        task=netmiko_send_command,
        command_string="terminal length 0",
        read_timeout=30,
    ))
    if failed:
        return failed

    result = nr.run(
        task=netmiko_send_command,
        command_string="show running-configuration",
        use_timing=True,
        read_timeout=120,
    )

    output = {}

    for hostname, multi_result in result.items():
        task_result = multi_result[0]

        if task_result.failed:
            output[hostname] = {
                "failed": True,
                "result": str(task_result.result),
            }
            continue

        config = str(task_result.result)

        timestamp = datetime.now(
            timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")

        checksum = hashlib.sha256(
            config.encode("utf-8")
        ).hexdigest()

        output[hostname] = {
            "failed": False,
            "timestamp": timestamp,
            "checksum": checksum,
            "config": config,
        }

    return output
