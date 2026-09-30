"""
Device health read APIs and the manual fleet-check trigger.

Reads: Redis first (health:device:<id>, health:fleet:summary), PostgreSQL on a miss or
any Redis error, then the cache is repopulated with SET NX so a fresher worker write is
never overwritten. The API never connects to switches; it only enqueues worker tasks.
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app import cache
from app.celery_client import PUBLISH_RETRY_POLICY, celery_app
from app.db.devices import list_devices
from app.db.health import get_health_rows


logger = logging.getLogger("network_api.health")

router = APIRouter(prefix="/api/v1/health", tags=["health"])

DEVICE_KEY = "health:device:{device_id}"
FLEET_SUMMARY_KEY = "health:fleet:summary"
FLEET_LOCK_KEY = "health:fleet:lock"
MANUAL_COOLDOWN_KEY = "health:manual:cooldown"
DEVICES_LIST_KEY = "devices:list"

DEVICE_TTL_SECONDS = 90
FLEET_SUMMARY_TTL_SECONDS = 30
DEVICES_LIST_TTL_SECONDS = 300
MANUAL_COOLDOWN_SECONDS = 20

FLEET_TASK = "network_worker.health_check_all_devices"
STATUSES = ("healthy", "degraded", "down", "unknown")

UNKNOWN_HEALTH = {
    "status": "unknown",
    "last_check_at": None,
    "last_success_at": None,
    "response_time_ms": None,
    "tcp_reachable": None,
    "ssh_reachable": None,
    "cli_reachable": None,
    "last_error": None,
    "consecutive_failures": 0,
    "last_status_change_at": None,
}


def cached_devices():
    """Device inventory (no health). Short-lived cache; PostgreSQL on miss."""
    devices = cache.get_json(DEVICES_LIST_KEY)

    if devices is None:
        devices = list_devices()
        cache.set_json(DEVICES_LIST_KEY, devices, DEVICES_LIST_TTL_SECONDS)

    return devices


def health_by_device(device_ids):
    """Current health per device id: Redis hits, PostgreSQL for the misses."""
    device_ids = list(device_ids)
    cached = cache.mget_json([DEVICE_KEY.format(device_id=i) for i in device_ids])

    result = {}
    misses = []

    for device_id, value in zip(device_ids, cached):
        if value is None:
            misses.append(device_id)
        else:
            result[device_id] = value

    if misses:
        rows = get_health_rows(misses)

        for device_id in misses:
            row = rows.get(device_id)

            if row is None:
                # Never checked: report unknown, and don't cache it.
                result[device_id] = {"device_id": device_id, **UNKNOWN_HEALTH}
                continue

            result[device_id] = row
            cache.set_json(
                DEVICE_KEY.format(device_id=device_id),
                row,
                DEVICE_TTL_SECONDS,
                only_if_absent=True,
            )

    return result


def devices_with_health():
    """GET /api/v1/devices payload: inventory plus flattened health fields."""
    devices = cached_devices()
    health = health_by_device(device["id"] for device in devices)

    merged = []

    for device in devices:
        h = health.get(device["id"], UNKNOWN_HEALTH)
        merged.append(
            {
                **device,
                "health_status": h["status"],
                "last_check_at": h["last_check_at"],
                "last_success_at": h["last_success_at"],
                "response_time_ms": h["response_time_ms"],
                "tcp_reachable": h["tcp_reachable"],
                "ssh_reachable": h["ssh_reachable"],
                "cli_reachable": h["cli_reachable"],
                "last_error": h["last_error"],
            }
        )

    return merged


def _summarize(entries):
    summary = {"total": 0, **{status: 0 for status in STATUSES}, "last_updated": None}

    for entry in entries:
        if not entry["enabled"]:
            continue
        summary["total"] += 1
        summary[entry["status"]] = summary.get(entry["status"], 0) + 1
        checked = entry["last_check_at"]
        if checked and (summary["last_updated"] is None or checked > summary["last_updated"]):
            summary["last_updated"] = checked

    return summary


def _health_entries():
    devices = cached_devices()
    health = health_by_device(device["id"] for device in devices)

    return [
        {
            "device_id": device["id"],
            "hostname": device["hostname"],
            "management_ip": device["management_ip"],
            "platform": device["platform"],
            "enabled": device["enabled"],
            **{k: v for k, v in health.get(device["id"], UNKNOWN_HEALTH).items() if k != "device_id"},
        }
        for device in devices
    ]


@router.get("/devices")
def fleet_health():
    try:
        entries = _health_entries()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    summary = cache.get_json(FLEET_SUMMARY_KEY)

    if summary is None:
        summary = _summarize(entries)
        cache.set_json(FLEET_SUMMARY_KEY, summary, FLEET_SUMMARY_TTL_SECONDS, only_if_absent=True)

    return {
        **summary,
        "check_running": bool(cache.exists(FLEET_LOCK_KEY)),
        "devices": entries,
    }


@router.get("/devices/{device_id}")
def device_health(device_id: int):
    try:
        entry = next((e for e in _health_entries() if e["device_id"] == device_id), None)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if entry is None:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")

    return entry


@router.post("/check", status_code=202)
def request_fleet_check():
    # A sweep already in progress: nothing to enqueue.
    if cache.exists(FLEET_LOCK_KEY):
        return JSONResponse(
            status_code=409,
            content={"status": "running", "detail": "A fleet health check is already running"},
        )

    # Cooldown against rapid clicks. If Redis is down (None) the broker is too, and
    # send_task below fails with 503 anyway.
    if cache.claim(MANUAL_COOLDOWN_KEY, MANUAL_COOLDOWN_SECONDS) is False:
        retry_after = cache.ttl(MANUAL_COOLDOWN_KEY) or MANUAL_COOLDOWN_SECONDS
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "status": "cooldown",
                "detail": f"A health check was just requested; try again in {retry_after} s",
                "retry_after": retry_after,
            },
        )

    try:
        task = celery_app.send_task(
            FLEET_TASK,
            kwargs={"trigger": "manual"},
            retry=True,
            retry_policy=PUBLISH_RETRY_POLICY,
            expires=120,
        )
    except Exception as exc:
        logger.exception("Failed to enqueue manual fleet health check")
        raise HTTPException(status_code=503, detail=f"Task queue unavailable: {exc}")

    logger.info("fleet_health_manual_requested request_id=%s", task.id)

    return {"status": "queued", "request_id": task.id}
