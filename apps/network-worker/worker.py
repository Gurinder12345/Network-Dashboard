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
from db.health import list_enabled_devices
from health.checks import check_and_record, run_fleet_health_check
from db.topology import list_inventory, list_lldp_identities
from topology.discovery import discover_device, run_fleet_topology_discovery
from telemetry.collector import collect_and_record, run_fleet_metrics
from pcap.task import cleanup as cleanup_pcap, run_analysis as run_pcap_analysis

from ansible.run_os6 import run_os6_config_apply

from celery import Celery
from scheduling import offset_schedule
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
    get_show_output,
)



from db.jobs import (
    create_job,
    mark_job_failed,
    create_audit_event,
)

from health.cache import release_device_backup_lock
from tasks.config_backup import perform_config_backup, run_manual_backup

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

PCAP_TASK_SOFT_LIMIT_SECONDS = int(os.getenv("PCAP_TASK_SOFT_LIMIT_SECONDS", "900"))

app = Celery(
    "network_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

# Read by the single celery-beat Deployment only. "expires" drops a sweep that could not
# start within one interval, so a busy worker never builds up a backlog of health runs.
#
# Staggered on the wall clock so the three fleet sweeps never start together (each opens
# SSH sessions to every switch; the OS10 cores take ~13 s per login):
#   :00 every minute  health      :20 every minute  telemetry
#   :40 every 5 min   topology (minutes 0, 5, 10, ...)
HEALTH_SCHEDULE = offset_schedule(60, 0)
METRICS_SCHEDULE = offset_schedule(60, 20)
TOPOLOGY_SCHEDULE = offset_schedule(300, 40)
PCAP_CLEANUP_SCHEDULE = offset_schedule(3600, 50)

app.conf.beat_schedule = {
    "fleet-health-every-60s": {
        "task": "network_worker.health_check_all_devices",
        "schedule": HEALTH_SCHEDULE,
        "kwargs": {"trigger": "schedule"},
        "options": {"expires": 55},
    },
}

# Automatic topology discovery ENABLED (v0.15.13): LLDP parsers validated against real
# output from Kenda-Core-1/-2 (OS10) and Kenda-HARO-IDF-A / HARO-SW-01 (OS6), and
# device identities confirmed by reciprocal LLDP (db/seeds/lldp_identity_confirmed.sql).
# Set back to False to stop scheduled discovery; the tasks stay callable manually.
TOPOLOGY_BEAT_ENABLED = True

if TOPOLOGY_BEAT_ENABLED:
    # Read-only LLDP collection; a run that cannot start within the interval is dropped.
    app.conf.beat_schedule["topology-discovery-every-5m"] = {
        "task": "network_worker.discover_topology_all_devices",
        "schedule": TOPOLOGY_SCHEDULE,
        "kwargs": {"trigger": "schedule"},
        "options": {"expires": 280},
    }

# CPU/memory telemetry ENABLED (v0.15.15): OS6/OS10 parsers built from real captures
# (Kenda-HARO-IDF-A, Kenda-Core-1) and validated live; a manual 10-device fleet run took
# 21 s (OS6 avg 5.7 s, OS10 avg 13.8 s) at concurrency 4, well inside the 60 s interval.
# metrics:fleet:lock prevents overlap. Set back to False to stop scheduled collection.
TELEMETRY_BEAT_ENABLED = True

# PCAP uploads: hourly cleanup at :00:50 (filesystem/DB only; no SSH, so it cannot load switches).
app.conf.beat_schedule["pcap-cleanup-hourly"] = {
    "task": "network_worker.cleanup_pcap_files",
    "schedule": PCAP_CLEANUP_SCHEDULE,
    "options": {"expires": 3000},
}

if TELEMETRY_BEAT_ENABLED:
    # Read-only; separate from health so a slow CLI never delays health polling.
    app.conf.beat_schedule["fleet-metrics-every-60s"] = {
        "task": "network_worker.collect_fleet_metrics",
        "schedule": METRICS_SCHEDULE,
        "kwargs": {"trigger": "schedule"},
        "options": {"expires": 55},
    }



@app.task(name="network_worker.os6_change_precheck")
def os6_change_precheck(
    target_host,
    config_lines,
    config_parents=None,
):


    running_config_snapshot = get_show_output(
        target_host,
        "show running-config",)


    dry_run = run_os6_config_check(
        target_host,
        config_lines,
        config_parents,
        running_config_snapshot=running_config_snapshot,
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
        target_host,config_snapshot=running_config_snapshot,
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
def os6_apply_approved_change(approval_id, claimed_by_api=False):
    approval = get_change_approval(approval_id)

    # The API claims approved -> applying atomically before enqueueing, so its
    # tasks expect "applying". Direct callers still pass an "approved" record
    # and are claimed below, as before.
    expected_status = "applying" if claimed_by_api else "approved"

    if approval["status"] != expected_status:
        raise ValueError(
            f"Approval {approval_id} is {approval['status']}, "
            f"expected {expected_status}"
        )

    try:
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

    except Exception as exc:
        # An API-claimed approval is already "applying"; fail it instead of
        # leaving it stuck. Nothing has been sent to the device at this point.
        if claimed_by_api:
            mark_approval_failed(approval_id)

            create_audit_event(
                job_id=approval["backup_job_id"],
                device_id=approval["device_id"],
                event_type="apply_failed",
                message=(
                    f"Approved configuration apply rejected before "
                    f"device changes (approval_id={approval_id}): {exc}"
                ),
            )

        raise

    if not claimed_by_api:
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
            config_parents,
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
def ansible_os6_config_check(
    target_host,
    config_lines,
    config_parents=None,
):
    return run_os6_config_check(
        target_host,
        config_lines,
        config_parents,
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


@app.task(name="network_worker.device_health_check")
def device_health_check(device_id):
    # Read-only: TCP/22, SSH login and "show version". Never changes configuration.
    device = next(
        (d for d in list_enabled_devices() if d["id"] == int(device_id)),
        None,
    )

    if device is None:
        return {
            "device_id": device_id,
            "skipped": True,
            "reason": "Device not found or disabled",
        }

    return check_and_record(device)


@app.task(name="network_worker.health_check_all_devices")
def health_check_all_devices(trigger="schedule"):
    return run_fleet_health_check(trigger=trigger)


@app.task(name="network_worker.discover_topology_device")
def discover_topology_device(device_id):
    # Read-only: one LLDP show command. Never changes configuration.
    device = next(
        (d for d in list_enabled_devices() if d["id"] == int(device_id)),
        None,
    )

    if device is None:
        return {
            "device_id": device_id,
            "skipped": True,
            "reason": "Device not found or disabled",
        }

    return discover_device(device, list_inventory(), list_lldp_identities())


@app.task(name="network_worker.discover_topology_all_devices")
def discover_topology_all_devices(trigger="schedule"):
    return run_fleet_topology_discovery(trigger=trigger)


@app.task(name="network_worker.collect_device_metrics")
def collect_device_metrics(device_id):
    # Read-only CPU/memory telemetry for one device. Never changes health or configuration.
    device = next(
        (d for d in list_enabled_devices() if d["id"] == int(device_id)),
        None,
    )

    if device is None:
        return {
            "device_id": device_id,
            "skipped": True,
            "reason": "Device not found or disabled",
        }

    sample = collect_and_record(device)
    return {**sample, "collected_at": sample["collected_at"].isoformat()}


@app.task(name="network_worker.collect_fleet_metrics")
def collect_fleet_metrics(trigger="schedule"):
    return run_fleet_metrics(trigger=trigger)


@app.task(
    name="network_worker.analyze_pcap",
    soft_time_limit=PCAP_TASK_SOFT_LIMIT_SECONDS,
    time_limit=PCAP_TASK_SOFT_LIMIT_SECONDS + 60,
)
def analyze_pcap(analysis_id):
    # Reads an uploaded capture file with TShark; no live capture, no device access.
    return run_pcap_analysis(analysis_id)


@app.task(name="network_worker.cleanup_pcap_files")
def cleanup_pcap_files():
    # Backstop for the per-analysis deletion: expired/orphan uploads, stale analyses.
    return cleanup_pcap()


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
def backup_running_config_task(
    target_host,
    config_snapshot=None,
):
    # Change-workflow backup (precheck passes its running-config snapshot). The collection,
    # storage and records are the shared core in tasks/config_backup.py.
    device = get_device_by_hostname(target_host)

    device_id = device["id"]
    platform = device["platform"]

    job_id = create_job(
        device_id=device_id,
        job_type="config_backup",
        requested_by="celery",
    )

    try:
        backup = perform_config_backup(
            target_host,
            device_id,
            platform,
            job_id,
            config_snapshot=config_snapshot,
        )

        return {
            "job_id": str(job_id),
            "status": "success",
            "hostname": target_host,
            "platform": platform,
            "storage_path": backup["storage_path"],
            "checksum": backup["checksum"],
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


@app.task(name="network_worker.manual_backup_device")
def manual_backup_device(device_id, job_id, lock_token=None):
    # Read-only "Backup Now": no approval, precheck or configuration commands. The API
    # created job_id (queued) and holds the per-device lock; the job row carries the outcome.
    try:
        return run_manual_backup(int(device_id), job_id)
    finally:
        release_device_backup_lock(device_id, lock_token)
