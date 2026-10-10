"""
Interface Monitoring V1 read API. Never contacts switches (the worker collects on its own
schedule with read-only `show` commands) and never changes device health.

GET /api/v1/devices/{id}/interfaces                       summary + every interface: ONE request
                                                          draws the whole Interfaces tab
GET /api/v1/devices/{id}/interfaces/summary               the same without the interface list
GET /api/v1/devices/{id}/interfaces/{interface_id}        one interface (latest state)
GET /api/v1/devices/{id}/interfaces/{interface_id}/metrics?range=1h|6h|24h|7d  (or from/to, max 7 days)
                                                          history, only when a chart is opened

Latest state: Redis first -- one MGET reads the device snapshot and the last-attempt
record, no PostgreSQL query on a hit. Miss (absent/expired key, unknown schema version,
Redis down): one PostgreSQL query rebuilds the same snapshot, which is then cached for a
short time (SET NX, never overwriting the worker's fresher write).

Freshness is computed from collected_at, never from Redis expiry: older than
INTERFACE_STALE_AFTER_SECONDS (default 2 x poll + 60 s) -> stale=True. Stale data is
labelled stale; it is never turned into "down".
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import redis
from fastapi import APIRouter, HTTPException, Query

from app import cache
from app.db.devices import get_device_by_id
from app.db.interfaces import get_interface, interface_history, latest_device_interfaces

logger = logging.getLogger("network_api.interfaces")

router = APIRouter(prefix="/api/v1/devices", tags=["interfaces"])

SCHEMA_VERSION = 1
SNAPSHOT_KEY = "interface:latest:device:{device_id}"
ATTEMPT_KEY = "interface:attempt:device:{device_id}"
POLL_SECONDS = int(os.getenv("INTERFACE_POLL_SECONDS", "300"))
STALE_AFTER_SECONDS = int(os.getenv("INTERFACE_STALE_AFTER_SECONDS", str(2 * POLL_SECONDS + 60)))
MAX_DELTA_SECONDS = int(os.getenv("INTERFACE_MAX_DELTA_SECONDS", str(3 * POLL_SECONDS)))
READ_THROUGH_TTL_SECONDS = 60
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600}
MAX_RANGE_SECONDS = RANGES["7d"]
MAX_HISTORY_ROWS = 5000  # 7 days at 5 minutes is 2016

SUMMARY_KEYS = ("total", "up", "down", "admin_down", "unknown", "erroring", "trunks", "access_ports")
COUNTERS = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "crc_errors",
            "input_discards", "output_discards")


# ---- shared semantics (mirror the worker's interfaces/utilization.py) --------------------------
def status_of(admin_status, oper_status):
    if admin_status == "down":
        return "admin_down"
    if oper_status == "up":
        return "up"
    if oper_status == "down" and admin_status == "up":
        return "down"
    return "unknown"


def _delta(previous, current):
    if previous is None or current is None or current < previous:
        return None
    return current - previous


def error_deltas(previous, current):
    """errors/CRC/discard increases between two valid consecutive samples, else None."""
    result = {"errors_delta": None, "crc_delta": None, "discards_delta": None, "erroring": False}
    if not previous or not current:
        return result
    elapsed = (current["collected_at"] - previous["collected_at"]).total_seconds()
    if elapsed <= 0 or elapsed > MAX_DELTA_SECONDS:
        return result

    def total(*fields):
        # Fields missing on either side are ignored; any counter that went backwards
        # (clear/reboot) makes the whole delta unknown rather than an understated number.
        deltas = []
        for field in fields:
            before, after = previous.get(field), current.get(field)
            if before is None or after is None:
                continue
            if after < before:
                return None
            deltas.append(after - before)
        return sum(deltas) if deltas else None

    result.update(errors_delta=total("rx_errors", "tx_errors"), crc_delta=total("crc_errors"),
                  discards_delta=total("input_discards", "output_discards"))
    result["erroring"] = bool((result["errors_delta"] or 0) > 0 or (result["crc_delta"] or 0) > 0)
    return result


def summarize(interfaces):
    summary = dict.fromkeys(SUMMARY_KEYS, 0)
    for item in interfaces:
        summary["total"] += 1
        summary[item["status"]] += 1
        summary["erroring"] += 1 if item["erroring"] else 0
        summary["trunks"] += 1 if item["mode"] == "trunk" else 0
        summary["access_ports"] += 1 if item["mode"] == "access" else 0
    return summary


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


# ---- snapshot sources ---------------------------------------------------------------------------
def _read_cache(device_id):
    """(snapshot | None, attempt | None, reason). One Redis round trip."""
    keys = [SNAPSHOT_KEY.format(device_id=device_id), ATTEMPT_KEY.format(device_id=device_id)]
    try:
        raw_snapshot, raw_attempt = cache._redis().mget(keys)
    except redis.RedisError as exc:
        logger.warning("interface_cache_error action=read device_id=%s error=%s fallback=postgresql",
                       device_id, type(exc).__name__)
        return None, None, "redis_error"
    try:
        attempt = json.loads(raw_attempt) if raw_attempt else None
        snapshot = json.loads(raw_snapshot) if raw_snapshot else None
    except ValueError:
        return None, None, "invalid_json"
    if snapshot is None:
        return None, attempt, "absent"
    if snapshot.get("schema_version") != SCHEMA_VERSION or snapshot.get("device_id") != device_id:
        return None, attempt, f"schema_version_{snapshot.get('schema_version')}"
    return snapshot, attempt, "hit"


def snapshot_from_db(device):
    """The worker's snapshot shape, rebuilt from PostgreSQL in one query."""
    rows = latest_device_interfaces(device["id"], MAX_DELTA_SECONDS)
    if not rows:
        return None
    interfaces = []
    for inv, current, previous in rows:
        latest = current or {}
        interfaces.append({
            "id": inv["id"], "name": inv["interface_name"], "canonical_name": inv["canonical_name"],
            "description": inv["description"], "type": inv["interface_type"], "role": inv["role"],
            "admin_status": inv["admin_status"], "oper_status": inv["oper_status"],
            "status": status_of(inv["admin_status"], inv["oper_status"]),
            "speed_bps": inv["speed_bps"], "duplex": inv["duplex"], "mode": inv["mode"],
            "access_vlan": inv["access_vlan"], "native_vlan": inv["native_vlan"],
            "allowed_vlans": inv["allowed_vlans"], "port_channel": inv["port_channel"], "mtu": inv["mtu"],
            "last_state_change": _iso(inv["last_state_change"]),
            **{field: latest.get(field) for field in COUNTERS},
            "rx_utilization_pct": latest.get("rx_utilization_pct"),
            "tx_utilization_pct": latest.get("tx_utilization_pct"),
            **error_deltas(previous, current),
        })
    collected_at = max(inv["last_seen"] for inv, _, _ in rows)
    return {
        "schema_version": SCHEMA_VERSION, "device_id": device["id"], "hostname": device["hostname"],
        "platform": device["platform"], "collected_at": _iso(collected_at),
        "collection_status": None,  # per-collection outcome is kept in Redis only (last attempt)
        "problems": [], "poll_interval_seconds": POLL_SECONDS,
        "summary": summarize(interfaces), "interfaces": interfaces,
    }


def _device_or_404(device_id):
    try:
        device = get_device_by_id(device_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")
    return device


def load_latest(device_id):
    """(snapshot | None, attempt | None, source, device | None)."""
    snapshot, attempt, reason = _read_cache(device_id)
    if snapshot is not None:
        logger.info("interface_cache_hit device_id=%s", device_id)
        return snapshot, attempt, "cache", None

    logger.info("interface_cache_miss device_id=%s reason=%s", device_id, reason)
    device = _device_or_404(device_id)
    try:
        snapshot = snapshot_from_db(device)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # e.g. migration 009 not applied yet
        logger.warning("interface_db_read_failed device_id=%s error=%s", device_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Interface data is not available")
    logger.info("interface_cache_fallback_db device_id=%s found=%s", device_id, snapshot is not None)
    if snapshot is not None and reason == "absent":
        # Short read-through; NX so a fresher worker snapshot is never replaced.
        cache.set_json(SNAPSHOT_KEY.format(device_id=device_id), snapshot, READ_THROUGH_TTL_SECONDS, only_if_absent=True)
    return snapshot, attempt, "database", device


def _freshness(collected_at, now=None):
    if not collected_at:
        return None, False
    now = now or datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(collected_at)).total_seconds()
    return int(max(age, 0)), age > STALE_AFTER_SECONDS


def build_response(device_id, include_interfaces=True):
    snapshot, attempt, source, device = load_latest(device_id)
    if snapshot is None:
        device = device or _device_or_404(device_id)
        return {
            "device_id": device_id, "hostname": device["hostname"], "platform": device["platform"],
            "source": source, "status": "not_collected", "collected_at": None, "age_seconds": None, "stale": False,
            "stale_after_seconds": STALE_AFTER_SECONDS, "poll_interval_seconds": POLL_SECONDS,
            "collection_status": None, "problems": [], "last_attempt": attempt,
            "summary": None, **({"interfaces": []} if include_interfaces else {}),
        }
    age, stale = _freshness(snapshot["collected_at"])
    response = {
        "device_id": device_id, "hostname": snapshot["hostname"], "platform": snapshot["platform"],
        "source": source, "status": "stale" if stale else "ok", "collected_at": snapshot["collected_at"],
        "age_seconds": age, "stale": stale, "stale_after_seconds": STALE_AFTER_SECONDS,
        "poll_interval_seconds": snapshot.get("poll_interval_seconds", POLL_SECONDS),
        "collection_status": snapshot.get("collection_status"), "problems": snapshot.get("problems", []),
        "last_attempt": attempt, "summary": snapshot["summary"],
    }
    if include_interfaces:
        response["interfaces"] = snapshot["interfaces"]
    return response


# ---- routes ---------------------------------------------------------------------------------------
@router.get("/{device_id}/interfaces")
def device_interfaces(device_id: int):
    return build_response(device_id)


@router.get("/{device_id}/interfaces/summary")
def device_interfaces_summary(device_id: int):
    return build_response(device_id, include_interfaces=False)


@router.get("/{device_id}/interfaces/{interface_id}")
def device_interface(device_id: int, interface_id: int):
    snapshot, _, _, device = load_latest(device_id)
    if snapshot is not None:
        match = next((i for i in snapshot["interfaces"] if i["id"] == interface_id), None)
        if match is not None:
            age, stale = _freshness(snapshot["collected_at"])
            return {"device_id": device_id, "collected_at": snapshot["collected_at"], "age_seconds": age,
                    "stale": stale, "interface": match}
    # Not in the latest collection (e.g. removed): the stored inventory row, clearly marked.
    device = device or _device_or_404(device_id)
    inventory = get_interface(device_id, interface_id)
    if inventory is None:
        raise HTTPException(status_code=404, detail=f"Interface {interface_id} not found on device {device_id}")
    return {"device_id": device_id, "collected_at": _iso(inventory["last_seen"]), "age_seconds": None, "stale": True,
            "not_in_latest_collection": True,
            "interface": {**{k: _iso(v) for k, v in inventory.items()},
                          "status": status_of(inventory["admin_status"], inventory["oper_status"])}}


@router.get("/{device_id}/interfaces/{interface_id}/metrics")
def device_interface_metrics(
    device_id: int,
    interface_id: int,
    range_: Literal["1h", "6h", "24h", "7d"] = Query("24h", alias="range"),
    start: Optional[datetime] = Query(None, alias="from"),
    end: Optional[datetime] = Query(None, alias="to"),
):
    now = datetime.now(timezone.utc)
    custom = start is not None or end is not None
    if custom:
        end = end or now
        start = start or end - timedelta(seconds=RANGES[range_])
        if start.tzinfo is None or end.tzinfo is None:
            raise HTTPException(status_code=422, detail="from/to must include a timezone (ISO 8601, e.g. ...Z)")
        if end <= start:
            raise HTTPException(status_code=422, detail="'to' must be after 'from'")
        if (end - start).total_seconds() > MAX_RANGE_SECONDS:
            raise HTTPException(status_code=422, detail="The maximum history range is 7 days")
    else:
        end, start = now, now - timedelta(seconds=RANGES[range_])

    try:
        if get_interface(device_id, interface_id) is None:
            raise HTTPException(status_code=404, detail=f"Interface {interface_id} not found on device {device_id}")
        rows = interface_history(device_id, interface_id, start, end, MAX_HISTORY_ROWS)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    samples, previous = [], None
    for row in rows:
        deltas = error_deltas(previous, row)
        samples.append({"collected_at": row["collected_at"].isoformat(), "rx_utilization_pct": row["rx_utilization_pct"],
                        "tx_utilization_pct": row["tx_utilization_pct"], "errors_delta": deltas["errors_delta"],
                        "crc_delta": deltas["crc_delta"], "discards_delta": deltas["discards_delta"]})
        previous = row
    return {"device_id": device_id, "interface_id": interface_id, "range": None if custom else range_,
            "from": start.isoformat(), "to": end.isoformat(), "interval_seconds": POLL_SECONDS,
            "truncated": len(rows) >= MAX_HISTORY_ROWS, "samples": samples}


def fleet_summaries(device_ids):
    """
    {device_id: summary | None} for many devices from ONE Redis MGET (no per-device calls).
    Building block for a future GET /api/v1/interfaces/summary; devices whose snapshot is
    missing come back as None so a caller can batch a single PostgreSQL fallback.
    """
    keys = [SNAPSHOT_KEY.format(device_id=d) for d in device_ids]
    result = {}
    for device_id, snapshot in zip(device_ids, cache.mget_json(keys)):
        valid = snapshot and snapshot.get("schema_version") == SCHEMA_VERSION
        result[device_id] = {"collected_at": snapshot["collected_at"], **snapshot["summary"]} if valid else None
    return result
