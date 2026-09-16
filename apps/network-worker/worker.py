import os
from celery import Celery
from tasks.dell_os6 import run_show_command, backup_running_config
from tasks.dell_os6 import run_show_command
from vault.client import get_device_credentials
from db.client import get_connection
from tasks.dell_os6 import backup_running_config
from db.devices import get_device_by_hostname
from nornir import InitNornir


from db.jobs import (
    create_job,
    mark_job_success,
    mark_job_failed,
    create_backup_record,
    create_audit_event,
)

from tasks.dell_os6 import (
    run_show_command as run_os6_show_command,
    backup_running_config as backup_os6_running_config,
)


from tasks.dell_os10 import (
    run_show_command as run_os10_show_command,
    get_running_config as get_os10_running_config,
    backup_running_config as backup_os10_running_config,
)

REDIS_URL = os.getenv(
    "CELERY_BROKER_URL",
    "redis://redis-master.network-platform.svc.cluster.local:6379/0",
)

app = Celery(
    "network_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

@app.task(name="network_worker.os10_show_version")
def os10_show_version(target_host):
    return run_os10_show_command(
        target_host,
        "show version",
    )


@app.task(name="network_worker.health_check")
def health_check():
    return {
        "status": "ok",
        "worker": "network-automation-worker",
    }


@app.task(name="network_worker.vault_test")
def vault_test(target_host):

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

    if target_host not in nr.inventory.hosts:
        raise ValueError(
            f"Device not found in inventory: {target_host}"
        )

    host = nr.inventory.hosts[target_host]

    credential_path = host.data["credential_path"]

    credentials = get_device_credentials(
        credential_path
    )

    return {
        "status": "ok",
        "target_host": target_host,
        "platform": host.platform,
        "credential_path": credential_path,
        "available_fields": list(credentials.keys()),
    }

@app.task(name="network_worker.os6_show_version")
def os6_show_version(target_host):
    return run_show_command(
        target_host,
        "show version",
    )




@app.task(name="network_worker.db_health_check")
def db_health_check():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user;")
            database, user = cur.fetchone()

    return {
        "status": "ok",
        "database": database,
        "user": user,
    }


@app.task(name="network_worker.os6_show_interfaces_status")
def os6_show_interfaces_status(target_host):
    return run_show_command(
        target_host,
        "show interfaces status",
    )


@app.task(name="network_worker.os6_show_vlan")
def os6_show_vlan(target_host):
    return run_show_command(
        target_host,
        "show vlan",
    )


@app.task(name="network_worker.os6_show_ip_interface")
def os6_show_ip_interface(target_host):
    return run_show_command(
        target_host,
        "show ip interface",
    )


@app.task(name="network_worker.os6_show_spanning_tree")
def os6_show_spanning_tree(target_host):
    return run_show_command(
        target_host,
        "show spanning-tree",
    )
@app.task(name="network_worker.os10_show_interfaces_status")
def os10_show_interfaces_status(target_host):
    return run_os10_show_command(
        target_host,
        "show interface status",
    )


@app.task(name="network_worker.os10_show_vlan")
def os10_show_vlan(target_host):
    return run_os10_show_command(
        target_host,
        "show vlan",
    )


@app.task(name="network_worker.os10_show_ip_interface")
def os10_show_ip_interface(target_host):
    return run_os10_show_command(
        target_host,
        "show ip interface brief",
    )


@app.task(name="network_worker.os10_show_spanning_tree")
def os10_show_spanning_tree(target_host):
    return run_os10_show_command(
        target_host,
        "show spanning-tree",
    )


@app.task(name="network_worker.os10_show_running_config")
def os10_show_running_config(target_host):
    return get_os10_running_config(target_host)






@app.task(name="network_worker.backup_running_config")
def backup_running_config_task(target_host):
    device = get_device_by_hostname(target_host)

    device_id = device["id"]
    platform = device["platform"]

    job_id = create_job(
        device_id=device_id,
        job_type="config_backup",
        requested_by="celery",
    )

    try:
        create_audit_event(
            job_id=job_id,
            device_id=device_id,
            event_type="backup_started",
            message=f"Running-config backup started for {target_host}",
        )

        if platform == "dell_os6":
            result = backup_os6_running_config(target_host)

        elif platform == "dell_os10":
            result = backup_os10_running_config(target_host)

        else:
            raise ValueError(
                f"Unsupported platform for backup: {platform}"
            )

        device_result = result[target_host]

        if device_result["failed"]:
            raise RuntimeError(
                device_result.get(
                    "result",
                    "Device backup failed",
                )
            )

        timestamp = device_result["timestamp"]
        checksum = device_result["checksum"]
        config = device_result["config"]

        backup_dir = f"/backups/{target_host}"
        backup_path = f"{backup_dir}/{timestamp}.cfg"

        import os

        os.makedirs(
            backup_dir,
            mode=0o700,
            exist_ok=True,
        )

        with open(backup_path, "w") as backup_file:
            backup_file.write(config)

        os.chmod(
            backup_path,
            0o600,
        )

        create_backup_record(
        job_id=job_id,
        device_id=device_id,
        storage_path=backup_path,
        checksum=checksum,
        )

        create_audit_event(
            job_id=job_id,
            device_id=device_id,
            event_type="backup_completed",
            message=f"Backup completed: {backup_path}",
        )

        mark_job_success(job_id)

        return {
            "job_id": str(job_id),
            "status": "success",
            "hostname": target_host,
            "platform": platform,
            "storage_path": backup_path,
            "checksum": checksum,
        }

    except Exception as exc:

        mark_job_failed(
            job_id,
            str(exc),
        )

        create_audit_event(
            job_id=job_id,
            device_id=device_id,
            event_type="backup_failed",
            message=str(exc),
        )

        raise
