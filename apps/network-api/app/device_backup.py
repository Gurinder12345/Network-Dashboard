"""
Manual per-device "Backup Now" and job status.

POST /api/v1/devices/{device_id}/backup  queue a read-only running-config backup (no approval)
GET  /api/v1/jobs/{job_id}               job status, plus the backup it produced

The API validates the device, takes a short per-device Redis lock, creates the job row
(queued) and enqueues the worker task; it never connects to switches or reads Vault. The
job row in PostgreSQL is the status the UI polls. Authorization can later be added as a
router dependency without changing these handlers.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app import cache
from app.backup_download import download_filename, file_available
from app.celery_client import PUBLISH_RETRY_POLICY, celery_app
from app.db.audit import create_audit_event
from app.db.devices import get_device_by_id
from app.db.jobs import create_queued_job, get_job, mark_job_failed


logger = logging.getLogger("network_api.backup")

router = APIRouter(prefix="/api/v1", tags=["backups"])

MANUAL_BACKUP_TASK = "network_worker.manual_backup_device"
MANUAL_BACKUP_JOB_TYPE = "manual_backup"
SUPPORTED_PLATFORMS = ("dell_os6", "dell_os10")

# Held from request until the worker finishes (it releases it). Bounded: OS6 retries three
# times with long SSH timeouts, so a lost task frees the device after at most 15 minutes.
DEVICE_BACKUP_LOCK_KEY = "backup:device:{device_id}:lock"
DEVICE_BACKUP_LOCK_TTL_SECONDS = 900

# No dashboard authentication yet; replace with the signed-in user when auth exists.
REQUESTED_BY = "dashboard"


@router.post("/devices/{device_id}/backup", status_code=202)
def request_device_backup(device_id: int):
    try:
        device = get_device_by_id(device_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")

    if not device["enabled"]:
        raise HTTPException(status_code=422, detail=f"{device['hostname']} is disabled")

    if device["platform"] not in SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=422,
            detail=f"{device['hostname']} is {device['platform']}; backup supports {', '.join(SUPPORTED_PLATFORMS)}",
        )

    # Health is not checked: it can be stale, and a failed SSH attempt is recorded as a
    # normal backup failure.
    lock_key = DEVICE_BACKUP_LOCK_KEY.format(device_id=device_id)
    # The lock value is the job id, so a duplicate request can report which job is running.
    job_id = str(uuid.uuid4())

    # None = Redis unavailable; the broker shares that Redis, so send_task below fails with 503.
    if cache.claim(lock_key, DEVICE_BACKUP_LOCK_TTL_SECONDS, value=job_id) is False:
        return JSONResponse(
            status_code=409,
            content={
                "status": "running",
                "device_id": device_id,
                "job_id": cache.get_value(lock_key),
                "detail": "Backup already running for this device.",
            },
        )

    try:
        create_queued_job(job_id, device_id, MANUAL_BACKUP_JOB_TYPE, REQUESTED_BY)
    except Exception as exc:
        cache.release(lock_key, job_id)
        logger.exception("Failed to create manual backup job")
        raise HTTPException(status_code=500, detail=f"Could not create backup job: {type(exc).__name__}")

    try:
        task = celery_app.send_task(
            MANUAL_BACKUP_TASK,
            kwargs={"device_id": device_id, "job_id": job_id, "lock_token": job_id},
            retry=True,
            retry_policy=PUBLISH_RETRY_POLICY,
        )
    except Exception as exc:
        logger.exception("Failed to enqueue manual backup")
        cache.release(lock_key, job_id)
        try:
            mark_job_failed(job_id, "Task queue unavailable")
        except Exception:
            logger.exception("Failed to mark manual backup job failed")
        raise HTTPException(status_code=503, detail=f"Task queue unavailable: {type(exc).__name__}")

    try:
        create_audit_event(
            job_id=job_id,
            device_id=device_id,
            event_type="backup_requested",
            message=f"Manual backup requested for {device['hostname']} (source=manual, requested_by={REQUESTED_BY})",
        )
    except Exception:
        # Already queued; a missing audit row should not hide that from the user.
        logger.exception("Failed to record backup_requested audit event")

    logger.info("manual_backup_requested device_id=%s job_id=%s request_id=%s", device_id, job_id, task.id)

    return {
        "status": "queued",
        "device_id": device_id,
        "hostname": device["hostname"],
        "job_id": job_id,
        "request_id": task.id,
        "message": f"Backup queued for {device['hostname']}",
    }


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id")

    try:
        job = get_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    storage_path = job.pop("backup_storage_path")
    backup_created_at = job.pop("backup_created_at")
    backup_id = job.pop("backup_id")

    # Backup identity for download; the filesystem path itself stays server-side.
    job["backup"] = (
        {
            "backup_id": backup_id,
            "device_id": job["device_id"],
            "filename": download_filename(job["hostname"], job["device_id"], storage_path),
            "created_at": backup_created_at,
            "file_available": file_available(storage_path),
        }
        if backup_id is not None
        else None
    )

    return job
