"""
Latest-state interface cache (Redis logical DB 1, the existing application cache; never
the Celery broker DB 0). PostgreSQL stays authoritative; losing these keys loses nothing.

    interface:latest:device:<id>    ONE JSON snapshot per device: every monitored interface's
                                    latest state, counters, utilization, error deltas and the
                                    precomputed summary (schema_version 1). TTL 3 x poll interval.
                                    Written ONLY after a successful/partial collection that
                                    was stored in PostgreSQL; skips and failures never touch it.
    interface:attempt:device:<id>   outcome of the newest attempt (success/partial/failed/
                                    skipped_*), so the UI can say why data is not fresh. 24 h.

No historical samples are ever stored in Redis. Every call is best-effort: a Redis error is
logged (interface_cache_error) and reported to the caller, never raised.
"""

import json
import logging

import redis

from health.cache import _redis

logger = logging.getLogger("network_worker.interfaces")

SCHEMA_VERSION = 1
SNAPSHOT_KEY = "interface:latest:device:{device_id}"
ATTEMPT_KEY = "interface:attempt:device:{device_id}"
ATTEMPT_TTL_SECONDS = 86400


def write_snapshot(snapshot, ttl_seconds):
    """True when stored. The caller decides WHEN (never on skip/failure/empty data)."""
    if not snapshot.get("interfaces"):
        logger.warning("interface_cache_write_refused device_id=%s reason=empty_snapshot", snapshot.get("device_id"))
        return False
    try:
        _redis().setex(SNAPSHOT_KEY.format(device_id=snapshot["device_id"]), int(ttl_seconds), json.dumps(snapshot))
    except redis.RedisError as exc:
        logger.warning("interface_cache_error action=write_snapshot device_id=%s error=%s fallback=postgresql",
                       snapshot["device_id"], type(exc).__name__)
        return False
    logger.info("interface_cache_write device_id=%s interfaces=%s ttl=%s",
                snapshot["device_id"], len(snapshot["interfaces"]), ttl_seconds)
    return True


def write_attempt(device_id, attempt):
    try:
        _redis().setex(ATTEMPT_KEY.format(device_id=device_id), ATTEMPT_TTL_SECONDS, json.dumps(attempt))
        return True
    except redis.RedisError as exc:
        logger.warning("interface_cache_error action=write_attempt device_id=%s error=%s", device_id, type(exc).__name__)
        return False
