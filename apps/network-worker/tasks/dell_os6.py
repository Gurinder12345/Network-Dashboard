from nornir import InitNornir
from nornir.core.inventory import ConnectionOptions
from nornir_netmiko.tasks import netmiko_send_command
from datetime import datetime, timezone
from vault.client import get_os6_credentials
import hashlib

def get_nornir():
    credentials = get_os6_credentials()

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

    for host in nr.inventory.hosts.values():
        host.username = credentials["username"]
        host.password = credentials["password"]

        host.connection_options["netmiko"] = ConnectionOptions(
            extras={
                "secret": credentials["secret"],
            }
        )

    return nr




def backup_running_config():
    nr = get_nornir()

    result = nr.run(
        task=netmiko_send_command,
        command_string="show running-config",
        enable=True,
    )

    output = {}

    for hostname, multi_result in result.items():
        task_result = multi_result[0]
        config = str(task_result.result)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        checksum = hashlib.sha256(config.encode()).hexdigest()

        output[hostname] = {
            "failed": task_result.failed,
            "timestamp": timestamp,
            "checksum": checksum,
            "config": config,
        }

    return output


def backup_running_config():
    nr = get_nornir()

    result = nr.run(
        task=netmiko_send_command,
        command_string="show running-config",
        enable=True,
    )

    output = {}

    for hostname, multi_result in result.items():
        task_result = multi_result[0]

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        output[hostname] = {
            "failed": task_result.failed,
            "timestamp": timestamp,
            "config": str(task_result.result),
        }

    return output


def run_show_command(command):
    nr = get_nornir()

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
