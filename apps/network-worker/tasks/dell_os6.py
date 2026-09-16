from datetime import datetime, timezone
import hashlib
from nornir.core.filter import F
from nornir import InitNornir
from nornir.core.inventory import ConnectionOptions
from nornir_netmiko.tasks import netmiko_send_command
from vault.client import get_device_credentials


def get_nornir(target_host):
    nr = InitNornir(
        inventory={
            "plugin": "SimpleInventory",
            "options": {
                "host_file": "/app/inventory/hosts.yaml",
                "group_file": "/app/inventory/groups.yaml",
                "defaults_file": "/app/inventory/defaults.yaml",
            },
        }
    )

    nr = nr.filter(F(name=target_host))

    if len(nr.inventory.hosts) == 0:
        raise ValueError(f"Device not found in inventory: {target_host}")

    for host in nr.inventory.hosts.values():
        if host.platform != "dell_os6":
            raise ValueError(
                f"{host.name} is platform {host.platform}, not dell_os6"
            )

        credential_path = host.data["credential_path"]
        credentials = get_device_credentials(credential_path)

        host.username = credentials["username"]
        host.password = credentials["password"]

        host.connection_options["netmiko"] = ConnectionOptions(
            extras={
                "secret": credentials["secret"],
            }
        )

    return nr


def run_show_command(target_host, command):
    nr = get_nornir(target_host)

    result = nr.run(
        task=netmiko_send_command,
        command_string=command,
        enable=True,
    )

    output = {}

    for hostname, multi_result in result.items():
        task_result = multi_result[0]

        output[hostname] = {
            "failed": task_result.failed,
            "result": str(task_result.result),
        }

    return output





def backup_running_config(target_host):
    nr = get_nornir(target_host)

    result = nr.run(
        task=netmiko_send_command,
        command_string="show running-config",
        enable=True,
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
