import os
from celery import Celery
from tasks.dell_os6 import run_show_command, backup_running_config
from tasks.dell_os6 import run_show_command
from vault.client import get_os6_credentials
from db.client import get_connection
from tasks.dell_os6 import backup_running_config




from db.jobs import (
    create_job,
    mark_job_success,
    mark_job_failed,
    create_backup_record,
    create_audit_event,
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

@app.task(name="network_worker.os6_backup_running_config")
def os6_backup_running_config():
    device_id = 1
    hostname = "n3224-test-01"

    job_id = create_job(
        device_id=device_id,
        job_type="backup_running_config",
        requested_by="system",
    )

    create_audit_event(
        job_id=job_id,
        device_id=device_id,
        event_type="backup_started",
        message=f"Running-config backup started for {hostname}",
    )

    try:
        result = backup_running_config()

        device_result = result[hostname]

        if device_result["failed"]:
            raise RuntimeError("Running-config backup failed")

        storage_path = (
            f"/backups/{hostname}/"
            f"{device_result['timestamp']}.cfg"
        )
        

 
        backup_directory = os.path.dirname(storage_path)

        os.makedirs(
                   backup_directory,
                  mode=0o750,
                  exist_ok=True,
                              )

        with open(storage_path, "w", encoding="utf-8") as backup_file:
             backup_file.write(device_result["config"])

        os.chmod(storage_path, 0o600)

        create_backup_record(
            job_id=job_id,
            device_id=device_id,
            storage_path=storage_path,
            checksum=device_result["checksum"],
        )

        create_audit_event(
            job_id=job_id,
            device_id=device_id,
            event_type="backup_completed",
            message=f"Running-config backup completed for {hostname}",
        )

        mark_job_success(job_id)

        return {
            "job_id": job_id,
            "status": "success",
            "hostname": hostname,
            "storage_path": storage_path,
            "checksum": device_result["checksum"],
        }

    except Exception as exc:
        mark_job_failed(job_id, str(exc))

        create_audit_event(
            job_id=job_id,
            device_id=device_id,
            event_type="backup_failed",
            message=str(exc),
        )

        raise

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
