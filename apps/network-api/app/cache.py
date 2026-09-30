"""
Best-effort Redis cache for fast dashboard reads.

PostgreSQL stays authoritative. Every function here treats a Redis error as a cache miss
and logs at most one warning per 30 s, so a Redis outage never turns into an API error.
"""

import json
import logging
import os
import time

import redis


logger = logging.getLogger("network_api.cache")

# Logical DB 1: separate from the Celery broker/result backend on DB 0.
REDIS_CACHE_URL = os.getenv(
    "REDIS_CACHE_URL",
    "redis://redis-master.network-platform.svc.cluster.local:6379/1",
)

_client = None
_last_warning_at = 0.0


def _redis():
    global _client

    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_CACHE_URL,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )

    return _client


def _warn(action, exc):
    global _last_warning_at
    now = time.monotonic()

    if now - _last_warning_at >= 30:
        _last_warning_at = now
        logger.warning(
            "redis_cache_unavailable action=%s error=%s fallback=postgresql",
            action,
            type(exc).__name__,
        )


def get_json(key):
    try:
        raw = _redis().get(key)
    except redis.RedisError as exc:
        _warn(f"get {key}", exc)
        return None

    return json.loads(raw) if raw else None


def mget_json(keys):
    if not keys:
        return []

    try:
        values = _redis().mget(keys)
    except redis.RedisError as exc:
        _warn("mget", exc)
        return [None] * len(keys)

    return [json.loads(value) if value else None for value in values]


def set_json(key, value, ttl_seconds, only_if_absent=False):
    """only_if_absent=True never overwrites a fresher value written by the worker."""
    try:
        _redis().set(key, json.dumps(value), ex=ttl_seconds, nx=only_if_absent)
    except redis.RedisError as exc:
        _warn(f"set {key}", exc)


def exists(key):
    """True/False, or None when Redis is unavailable."""
    try:
        return bool(_redis().exists(key))
    except redis.RedisError as exc:
        _warn(f"exists {key}", exc)
        return None


def claim(key, ttl_seconds, value="1"):
    """SET NX EX. True if claimed, False if already held, None if Redis is unavailable."""
    try:
        return bool(_redis().set(key, value, nx=True, ex=ttl_seconds))
    except redis.RedisError as exc:
        _warn(f"claim {key}", exc)
        return None


def get_value(key):
    try:
        return _redis().get(key)
    except redis.RedisError as exc:
        _warn(f"get {key}", exc)
        return None


# Delete only if the key still holds our token (never a newer holder's lock).
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


def release(key, token):
    try:
        _redis().eval(_RELEASE_SCRIPT, 1, key, token)
    except redis.RedisError as exc:
        _warn(f"release {key}", exc)


def ttl(key):
    try:
        remaining = _redis().ttl(key)
    except redis.RedisError as exc:
        _warn(f"ttl {key}", exc)
        return None

    return remaining if remaining and remaining > 0 else None
