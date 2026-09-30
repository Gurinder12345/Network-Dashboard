"""
Redis cache and coordination for device health.

Redis is never authoritative: every function here is best-effort. A Redis failure is
logged (without secrets) and the caller carries on with PostgreSQL as the source of truth.
Only health state is cached here -- never credentials, configs or backup contents.
"""

import json
import logging
import os
import time
import uuid

import redis


logger = logging.getLogger("network_worker.health")

# Separate logical DB from the Celery broker/result backend (db 0).
REDIS_CACHE_URL = os.getenv(
    "REDIS_CACHE_URL",
    "redis://redis-master.network-platform.svc.cluster.local:6379/1",
)

DEVICE_KEY = "health:device:{device_id}"
FLEET_SUMMARY_KEY = "health:fleet:summary"
FLEET_LOCK_KEY = "health:fleet:lock"

DEVICE_TTL_SECONDS = 90
FLEET_SUMMARY_TTL_SECONDS = 30
FLEET_LOCK_TTL_SECONDS = int(os.getenv("HEALTH_FLEET_LOCK_TTL_SECONDS", "240"))

# Delete the lock only if we still own it (another run may hold it after our TTL expired).
_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""

_client = None
_last_warning_at = 0.0


def _redis():
    global _client

    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_CACHE_URL,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )

    return _client


def _warn(action, exc):
    # At most one warning per 30 s, so a Redis outage does not flood logs every sweep.
    global _last_warning_at
    now = time.monotonic()

    if now - _last_warning_at >= 30:
        _last_warning_at = now
        logger.warning(
            "redis_cache_unavailable action=%s error=%s fallback=postgresql",
            action,
            type(exc).__name__,
        )


def cache_device_health(health):
    try:
        _redis().setex(
            DEVICE_KEY.format(device_id=health["device_id"]),
            DEVICE_TTL_SECONDS,
            json.dumps(health),
        )
    except redis.RedisError as exc:
        _warn("cache_device_health", exc)


def cache_fleet_summary(summary):
    try:
        _redis().setex(FLEET_SUMMARY_KEY, FLEET_SUMMARY_TTL_SECONDS, json.dumps(summary))
    except redis.RedisError as exc:
        _warn("cache_fleet_summary", exc)


class FleetLock:
    """
    SET NX EX lock around a fleet sweep.

    acquired=True  -> we own it and must release it
    acquired=False -> another sweep holds it; skip this run
    acquired=None  -> Redis unavailable; run without a lock (Beat's expiry and the
                      schedule interval make overlap unlikely, and sweeps are read-only)

    Defaults are the health sweep's key/TTL; topology discovery passes its own.
    """

    def __init__(self, key=FLEET_LOCK_KEY, ttl_seconds=FLEET_LOCK_TTL_SECONDS):
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.token = uuid.uuid4().hex
        self.acquired = None

    def __enter__(self):
        try:
            ok = _redis().set(self.key, self.token, nx=True, ex=self.ttl_seconds)
            self.acquired = bool(ok)
        except redis.RedisError as exc:
            _warn(f"acquire {self.key}", exc)
            self.acquired = None

        return self

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            try:
                _redis().eval(_RELEASE_LOCK_SCRIPT, 1, self.key, self.token)
            except redis.RedisError as release_exc:
                # The TTL still expires the lock.
                _warn("release_fleet_lock", release_exc)

        return False


# ---- Topology (same Redis DB 1; PostgreSQL stays authoritative) -------------------
TOPOLOGY_GRAPH_KEY = "topology:graph"
TOPOLOGY_LAST_DISCOVERY_KEY = "topology:last_discovery"
TOPOLOGY_LAST_DISCOVERY_TTL_SECONDS = 3600


def invalidate_topology_graph():
    """Drop the API's cached graph so the next read rebuilds it from PostgreSQL."""
    try:
        _redis().delete(TOPOLOGY_GRAPH_KEY)
    except redis.RedisError as exc:
        _warn("invalidate topology graph", exc)


def cache_topology_last_discovery(summary):
    try:
        _redis().setex(TOPOLOGY_LAST_DISCOVERY_KEY, TOPOLOGY_LAST_DISCOVERY_TTL_SECONDS, json.dumps(summary))
    except redis.RedisError as exc:
        _warn("cache topology last discovery", exc)
