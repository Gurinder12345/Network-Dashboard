import os
from celery import Celery

from tasks.dell_os6 import run_show_command
from vault.client import get_os6_credentials


REDIS_URL = os.getenv(
    "CELERY_BROKER_URL",
    "redis://redis-master.network-platform.svc.cluster.local:6379/0",
)

app = Celery(
    "network_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
)


@app.task(name="network_worker.health_check")
def health_check():
    return {
        "status": "ok",
        "worker": "network-automation-worker",
    }


@app.task(name="network_worker.vault_test")
def vault_test():
    credentials = get_os6_credentials()

    return {
        "status": "ok",
        "vault_authenticated": True,
        "secret_path": "kv/network/dell-os6",
        "available_fields": list(credentials.keys()),
    }


@app.task(name="network_worker.os6_show_version")
def os6_show_version():
    return run_show_command("show version")


@app.task(name="network_worker.os6_show_interfaces_status")
def os6_show_interfaces_status():
    return run_show_command("show interfaces status")


@app.task(name="network_worker.os6_show_vlan")
def os6_show_vlan():
    return run_show_command("show vlan")


@app.task(name="network_worker.os6_show_ip_interface")
def os6_show_ip_interface():
    return run_show_command("show ip interface")


@app.task(name="network_worker.os6_show_spanning_tree")
def os6_show_spanning_tree():
    return run_show_command("show spanning-tree")
