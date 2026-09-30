"""
Running-config backup core, shared by both backup paths:

  A. change workflow   os6_change_precheck -> backup_running_config_task (reuses its snapshot)
  B. manual            POST /api/v1/devices/{id}/backup -> manual_backup_device

One implementation collects the config with the platform's existing read-only command
(OS6 "show running-config", OS10 "show running-configuration"), writes
/backups/<hostname>/<timestamp>.cfg (0600), records the backups row and audit events and
marks the job successful. Configuration contents and credentials are never logged or
returned.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone

from db.devices import get_device_by_id
from db.jobs import (
    create_audit_event,
    create_backup_record,
    mark_job_failed,
    mark_job_running,
    mark_job_success,
)
from tasks.dell_os6 import backup_running_config as backup_os6_running_config
from tasks.dell_os10 import backup_running_config as backup_os10_running_config


logger = logging.getLogger("network_worker.backup")

BACKUP_ROOT = os.getenv("BACKUP_ROOT", "/backups")

COLLECTORS = {
    "dell_os6": backup_os6_running_config,
    "dell_os10": backup_os10_running_config,
}

SUPPORTED_PLATFORMS = tuple(COLLECTORS)


class UnsupportedPlatformError(ValueError):
    pass


class BackupStorageError(OSError):
    """Writing the backup file failed. str() is the original error's, so existing job messages are unchanged."""


def snapshot_result(config_snapshot):
    """Device result for a config the caller already read (precheck snapshot): no new SSH session."""
    return {
        "failed": False,
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "checksum": hashlib.sha256(config_snapshot.encode("utf-8")).hexdigest(),
        "config": config_snapshot,
    }


def collect_running_config(target_host, platform):
    collector = COLLECTORS.get(platform)

    if collector is None:
        raise UnsupportedPlatformError(f"Unsupported platform for backup: {platform}")

    return collector(target_host)[target_host]


def perform_config_backup(target_host, device_id, platform, job_id, config_snapshot=None, source=None):
    """
    Collect (or reuse) the running config, store it and record it. Raises on failure;
    each caller decides how to mark its job failed. `source` only annotates audit messages.
    """
    suffix = f" (source={source})" if source else ""

    create_audit_event(
        job_id=job_id,
        device_id=device_id,
        event_type="backup_started",
        message=f"Running-config backup started for {target_host}{suffix}",
    )

    if config_snapshot is not None:
        device_result = snapshot_result(config_snapshot)
    else:
        device_result = collect_running_config(target_host, platform)

    if device_result["failed"]:
        raise RuntimeError(device_result.get("result", "Device backup failed"))

    config = device_result["config"]

    backup_dir = f"{BACKUP_ROOT}/{target_host}"
    backup_path = f"{backup_dir}/{device_result['timestamp']}.cfg"

    try:
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)

        with open(backup_path, "w") as backup_file:
            backup_file.write(config)

        os.chmod(backup_path, 0o600)
    except OSError as exc:
        raise BackupStorageError(str(exc)) from exc

    record = create_backup_record(
        job_id=job_id,
        device_id=device_id,
        storage_path=backup_path,
        checksum=device_result["checksum"],
    )

    completed = f"Backup completed: {backup_path}"
    if source:
        completed += f" (source={source}, backup_id={record['backup_id']})"

    create_audit_event(
        job_id=job_id,
        device_id=device_id,
        event_type="backup_completed",
        message=completed,
    )

    mark_job_success(job_id)

    return {
        "backup_id": record["backup_id"],
        "created_at": record["created_at"],
        "storage_path": backup_path,
        "checksum": device_result["checksum"],
        "size_bytes": len(config.encode("utf-8")),
    }


# Matched against the lower-cased error text: Nornir/Netmiko failures arrive as strings
# (the OS6 retry wrapper returns "Command failed after 3 attempts. Last error: ...").
_ERROR_CATEGORIES = (
    (("authentication",), "SSH authentication failed"),
    (("vault",), "Credential lookup failed"),
    (
        ("timed out", "timeout", "unable to connect", "connection refused", "no route to host",
         "network is unreachable", "tcp connection", "connection reset", "name or service not known"),
        "Device unreachable",
    ),
)


def classify_backup_error(exc):
    """A short, safe reason for the UI/job record; never raw device output or secrets."""
    if isinstance(exc, UnsupportedPlatformError):
        return "Unsupported platform"

    if isinstance(exc, BackupStorageError):
        return "Backup storage error"

    if type(exc).__module__.startswith("hvac"):
        return "Credential lookup failed"

    text = f"{type(exc).__name__} {exc}".lower()

    if "enabled device not found" in text:
        return "Device not found or disabled"

    if "not found in inventory" in text:
        return "Device missing from worker inventory"

    for needles, reason in _ERROR_CATEGORIES:
        if any(needle in text for needle in needles):
            return reason

    return "Running-config command failed"


def run_manual_backup(device_id, job_id):
    """
    Body of the manual "Backup Now" task. The API created `job_id` as queued; this marks it
    running, then success/failed. Never raises for device errors: the job row carries the
    outcome the UI polls.
    """
    if not mark_job_running(job_id):
        # Already picked up (broker redelivery) or no longer queued: never back up twice.
        logger.info("manual_backup_skipped job_id=%s reason=not_queued", job_id)
        return {"status": "skipped", "job_id": job_id, "device_id": device_id}

    hostname = f"device {device_id}"

    try:
        device = get_device_by_id(device_id)
        hostname = device["hostname"]

        if device["platform"] not in SUPPORTED_PLATFORMS:
            raise UnsupportedPlatformError(f"Unsupported platform for backup: {device['platform']}")

        result = perform_config_backup(
            hostname,
            device["id"],
            device["platform"],
            job_id,
            source="manual",
        )

    except Exception as exc:
        reason = classify_backup_error(exc)
        # Exception type and category only: device output can be long and is not needed here.
        logger.warning(
            "manual_backup_failed device_id=%s job_id=%s reason=%r error_type=%s",
            device_id,
            job_id,
            reason,
            type(exc).__name__,
        )

        mark_job_failed(job_id, reason)
        create_audit_event(
            job_id=job_id,
            device_id=device_id,
            event_type="backup_failed",
            message=f"Manual backup failed for {hostname}: {reason} (source=manual)",
        )

        return {"status": "failed", "job_id": job_id, "device_id": device_id, "error": reason}

    logger.info(
        "manual_backup_completed device_id=%s job_id=%s backup_id=%s size_bytes=%s",
        device_id,
        job_id,
        result["backup_id"],
        result["size_bytes"],
    )

    return {
        "status": "success",
        "job_id": job_id,
        "device_id": device_id,
        "hostname": hostname,
        "backup_id": result["backup_id"],
        "created_at": result["created_at"],
    }
