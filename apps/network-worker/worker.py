import os
from db.approvals import (
    get_change_approval,
    verify_backup_for_device,
    mark_approval_applying,
    mark_approval_applied,
    mark_approval_failed,
)

from db.approvals import create_change_approval

from db.devices import get_device_by_id

from ansible.run_os6 import run_os6_config_apply

from celery import Celery
from tasks.dell_os6 import run_show_command, backup_running_config
from tasks.dell_os6 import run_show_command
from vault.client import get_device_credentials
from db.client import get_connection
from tasks.dell_os6 import backup_running_config
from db.devices import get_device_by_hostname
from nornir import InitNornir
from ansible.run_os6 import (
    run_os6_show_version,
    run_os6_config_check,
)



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



@app.task(name="network_worker.os6_change_precheck")
def os6_change_precheck(
    target_host,
    config_lines,
    config_parents=None,
):
    dry_run = run_os6_config_check(
        target_host,
        config_lines,
    )

    if not dry_run["would_change"]:
        return {
            "target_host": target_host,
            "status": "no_change_required",
            "dry_run": dry_run,
            "backup_required": False,
            "ready_for_approval": False,
        }

    device = get_device_by_hostname(target_host)

    backup_result = backup_running_config_task(
        target_host
    )

    if backup_result.get("status") != "success":
        raise RuntimeError(
            f"Backup failed for {target_host}"
        )

    approval = create_change_approval(
        device_id=device["id"],
        backup_job_id=backup_result["job_id"],
        config_lines=config_lines,
        config_parents=config_parents,
        requested_by="system",
    )

    return {
        "target_host": target_host,
        "status": "pending_approval",
        "dry_run": dry_run,
        "backup_required": True,
        "backup": backup_result,
        "approval": approval,
        "config_parents": config_parents,
        "ready_for_approval": True,
    }



@app.task(name="network_worker.os6_apply_approved_change")
def os6_apply_approved_change(approval_id):
    approval = get_change_approval(approval_id)

    if approval["status"] != "approved":
        raise ValueError(
            f"Approval {approval_id} is not approved"
        )

    device = get_device_by_id(
        approval["device_id"]
    )

    if device["platform"] != "dell_os6":
        raise ValueError(
            f"{device['hostname']} is not a dell_os6 device"
        )

    backup_check = verify_backup_for_device(
        device["id"],
        approval["backup_job_id"],
    )

    if not backup_check["valid"]:
        raise ValueError(
            f"Backup verification failed: "
            f"{backup_check['reason']}"
        )

    config_lines = approval["config_lines"]
    config_parents = approval.get("config_parents")

    if not config_lines:
        raise ValueError(
            "Approved configuration is empty"
        )

    # approved -> applying
    mark_approval_applying(approval_id)

    create_audit_event(
        job_id=approval["backup_job_id"],
        device_id=device["id"],
        event_type="apply_started",
        message=(
            f"Approved configuration apply started for "
            f"{device['hostname']} "
            f"(approval_id={approval_id})"
        ),
    )

    try:
        result = run_os6_config_apply(
            device["hostname"],
            config_lines,
            config_parents,
        )

        if result["returncode"] != 0:
            raise RuntimeError(
                f"Ansible apply failed for "
                f"STDOUT:\n{result['stdout']}\n"
                f"STDERR:\n{result['stderr']}"
            )

        # Post-check: verify approved config is now present
        post_check = run_os6_config_check(
            device["hostname"],
            config_lines,
        )

        if post_check["would_change"]:
            raise RuntimeError(
                f"Post-check failed for "
                f"{device['hostname']}: "
                f"configuration is not present after apply"
            )

        # applying -> applied
        mark_approval_applied(approval_id)

        create_audit_event(
            job_id=approval["backup_job_id"],
            device_id=device["id"],
            event_type="apply_completed",
            message=(
                f"Approved configuration apply completed for "
                f"{device['hostname']} "
                f"(approval_id={approval_id})"
            ),
        )

        return {
            "approval_id": approval_id,
            "status": "applied",
            "target_host": device["hostname"],
            "approved_by": approval["approved_by"],
            "backup_job_id": approval["backup_job_id"],
            "config_lines": config_lines,
            "ansible_result": result,
            "config_parents": config_parents,
            "post_check": post_check,
        }

    except Exception as exc:
        # applying -> failed
        mark_approval_failed(approval_id)

        create_audit_event(
            job_id=approval["backup_job_id"],
            device_id=device["id"],
            event_type="apply_failed",
            message=(
                f"Approved configuration apply failed for "
                f"{device['hostname']} "
                f"(approval_id={approval_id}): {exc}"
            ),
        )

        raise





@app.task(name="network_worker.ansible_os6_show_version")
def ansible_os6_show_version(target_host):
    return run_os6_show_version(target_host)




@app.task(name="network_worker.ansible_os6_config_check")
def ansible_os6_config_check(target_host, config_lines):
    return run_os6_config_check(
        target_host,
        config_lines,
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
