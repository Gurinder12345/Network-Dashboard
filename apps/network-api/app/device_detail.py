"""
Device detail and CPU/memory telemetry reads.

GET /api/v1/devices/{device_id}                  inventory + health + latest telemetry + latest backup
GET /api/v1/devices/{device_id}/metrics/latest   newest telemetry sample (Redis, then PostgreSQL)
GET /api/v1/devices/{device_id}/metrics?range=   history for 1h | 6h | 24h | 7d only

Read-only: these never contact switches. Telemetry is collected by the worker on its own
schedule and never changes device health; `stale` here is presentation only.
"""

import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from app import cache
from app.backup_download import download_filename, file_available
from app.db.backups import latest_backup_for_device
from app.db.devices import get_device_by_id
from app.db.metrics import latest_metrics, metric_history
from app.health import UNKNOWN_HEALTH, health_by_device


router = APIRouter(prefix="/api/v1/devices", tags=["devices"])

METRICS_DEVICE_KEY = "metrics:device:{device_id}"

TELEMETRY_INTERVAL_SECONDS = int(os.getenv("TELEMETRY_INTERVAL_SECONDS", "60"))
# Two missed collections plus a little slack for a slow fleet run.
STALE_AFTER_SECONDS = int(os.getenv("TELEMETRY_STALE_AFTER_SECONDS", str(2 * TELEMETRY_INTERVAL_SECONDS + 30)))

# range -> (window seconds, bucket seconds or None for raw samples).
# 7d at 1/min would be ~10k points per device; 10-minute averages keep it near 1k.
RANGES = {
    "1h": (3600, None),
    "6h": (6 * 3600, None),
    "24h": (24 * 3600, None),
    "7d": (7 * 24 * 3600, 600),
}

METRIC_FIELDS = ("cpu_percent", "memory_percent", "memory_used_mb", "memory_total_mb", "uptime_seconds")


def _device_or_404(device_id):
    try:
        device = get_device_by_id(device_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")

    return device


def _is_stale(last_success_at, now=None):
    if not last_success_at:
        return True
    now = now or datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(last_success_at)).total_seconds()
    return age > STALE_AFTER_SECONDS


def latest_telemetry(device):
    latest = cache.get_json(METRICS_DEVICE_KEY.format(device_id=device["id"]))

    if latest is None:
        latest = latest_metrics(device["id"])

    base = {
        "device_id": device["id"],
        "hostname": device["hostname"],
        "interval_seconds": TELEMETRY_INTERVAL_SECONDS,
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }

    if latest is None:
        # Never collected: no fake zeros, and not "stale" either.
        return {
            **base,
            "status": "not_collected",
            **{field: None for field in METRIC_FIELDS},
            "collected_at": None,
            "last_success_at": None,
            "stale": False,
            "error": None,
        }

    return {
        **base,
        "status": latest["status"],
        **{field: latest.get(field) for field in METRIC_FIELDS},
        "collected_at": latest["collected_at"],
        "last_success_at": latest.get("last_success_at"),
        "stale": _is_stale(latest.get("last_success_at")),
        "error": latest.get("error"),
    }


def _backup_summary(device):
    backup = latest_backup_for_device(device["id"])
    if backup is None:
        return None
    # Identity for download only; the filesystem path stays server-side.
    return {
        "backup_id": backup["id"],
        "created_at": backup["created_at"],
        "source": backup["job_type"],
        "filename": download_filename(device["hostname"], device["id"], backup["storage_path"]),
        "file_available": file_available(backup["storage_path"]),
    }


@router.get("/{device_id}")
def device_detail(device_id: int):
    device = _device_or_404(device_id)

    try:
        health = health_by_device([device_id]).get(device_id, UNKNOWN_HEALTH)
        telemetry = latest_telemetry(device)
        backup = _backup_summary(device)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        **device,
        # Not part of the inventory schema yet.
        "site": None,
        "role": None,
        "health": {k: v for k, v in health.items() if k != "device_id"},
        "telemetry": telemetry,
        "latest_backup": backup,
    }


@router.get("/{device_id}/metrics/latest")
def device_metrics_latest(device_id: int):
    device = _device_or_404(device_id)

    try:
        return latest_telemetry(device)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{device_id}/metrics")
def device_metrics_history(
    device_id: int,
    range_: Literal["1h", "6h", "24h", "7d"] = Query("24h", alias="range"),
):
    device = _device_or_404(device_id)
    seconds, bucket = RANGES[range_]

    try:
        samples = metric_history(device["id"], seconds, bucket)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "device_id": device["id"],
        "range": range_,
        "interval_seconds": TELEMETRY_INTERVAL_SECONDS,
        "bucket_seconds": bucket,
        "downsampled": bucket is not None,
        "samples": samples,
    }
