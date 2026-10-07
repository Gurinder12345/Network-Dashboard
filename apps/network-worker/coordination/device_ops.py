"""
Per-device operation coordination (Redis, logical DB 1, same as the health cache).

Problem: health, telemetry, topology, backups, prechecks and changes each open their own
SSH session. On the same switch (OS10 cores take ~13 s per login) overlapping sessions
caused authentication failures and false "degraded" states. This module gives each
device ONE operation slot; different devices are never serialized against each other.

Keys (all with a TTL; nothing here can outlive a crashed worker):

    device:<id>:operation           the slot: JSON {token, device_id, operation, priority,
                                    exclusive, owner, started_at, expires_at}
    device:<id>:operation:waiting   a higher-priority operation is waiting for the slot
                                    (short TTL, refreshed while it waits)
    device:<id>:change-in-progress  an approved change is executing on the device:
                                    JSON {token, change_id, job_id, started_at, expires_at}
    device:<id>:health-grace        first health failure seen during the change (epoch s)

Priorities (lower = more important):

    1  change workflow: apply (+ pre-apply backup, postcheck, post-change health check),
       precheck, manual backup                                      exclusive, wait
    2  health                                                       waits briefly
    3  telemetry                                                    skip when busy
    4  interface_poll (future)                                      skip when busy
    5  topology                                                     skip when busy

Nothing is preempted mid-session: a waiting higher-priority operation registers in
:waiting, so lower-priority collectors yield (skip) instead of starting, and it takes the
slot as soon as the current holder releases it (or its TTL expires).

Atomicity: acquire is one Lua script (check waiter, SET NX EX, register waiter); release
and refresh are Lua scripts that only act when the stored token is ours. A task can never
delete or extend another task's slot.

Redis unavailable (status "unavailable"): callers fail closed for change-workflow work,
skip low-priority collection, and health runs unlocked (it is the outage canary).
"""

import json
import logging
import os
import socket
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import redis

logger = logging.getLogger("network_worker.coordination")

REDIS_URL = os.getenv("REDIS_CACHE_URL", "redis://redis-master.network-platform.svc.cluster.local:6379/1")

OPERATION_KEY = "device:{device_id}:operation"
WAITING_KEY = "device:{device_id}:operation:waiting"
CHANGE_KEY = "device:{device_id}:change-in-progress"
GRACE_KEY = "device:{device_id}:health-grace"


def _env_int(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# Priority, exclusive flag, default slot TTL and default wait per operation type. TTLs
# exceed each operation's worst-case duration from its existing timeouts (see report):
#   health     TCP 2x3 s + 1 s, SSH 5+10 s, prompt 20 s, CLI 15 s   ~57 s   -> 90 s
#   telemetry  TCP 7 s, SSH ~35 s, 2 commands x 30 s                ~100 s  -> 150 s
#   topology   SSH ~35 s, LLDP read 60 s                            ~95 s   -> 180 s
#   precheck   running-config read (OS10 120 s; OS6 retries)        -> 600 s
#   backup     manual backup (OS6: 3 attempts x 120 s read)          -> 900 s (= API lock)
#   apply      per phase, refreshed by the owner at each phase boundary (see workflow)
OPERATIONS = {
    "apply": {"priority": 1, "exclusive": True, "ttl": _env_int("COORD_APPLY_TTL_SECONDS", 900), "wait": _env_int("COORD_APPLY_WAIT_SECONDS", 150)},
    "precheck": {"priority": 1, "exclusive": True, "ttl": _env_int("COORD_PRECHECK_TTL_SECONDS", 600), "wait": _env_int("COORD_PRECHECK_WAIT_SECONDS", 60)},
    "backup": {"priority": 1, "exclusive": True, "ttl": _env_int("COORD_BACKUP_TTL_SECONDS", 900), "wait": _env_int("COORD_BACKUP_WAIT_SECONDS", 60)},
    "health": {"priority": 2, "exclusive": False, "ttl": _env_int("COORD_HEALTH_TTL_SECONDS", 90), "wait": _env_int("COORD_HEALTH_WAIT_SECONDS", 15)},
    "telemetry": {"priority": 3, "exclusive": False, "ttl": _env_int("COORD_TELEMETRY_TTL_SECONDS", 150), "wait": 0},
    "interface_poll": {"priority": 4, "exclusive": False, "ttl": _env_int("COORD_INTERFACE_POLL_TTL_SECONDS", 150), "wait": 0},
    "topology": {"priority": 5, "exclusive": False, "ttl": _env_int("COORD_TOPOLOGY_TTL_SECONDS", 180), "wait": 0},
}
LOW_PRIORITY = ("telemetry", "interface_poll", "topology")
WAITER_TTL_SECONDS = 15
POLL_INTERVAL_SECONDS = 0.5
CHANGE_HEALTH_GRACE_SECONDS = _env_int("CHANGE_HEALTH_GRACE_SECONDS", 90)

# Per-process counters, also returned in sweep summaries (no Prometheus for this feature).
COUNTERS = Counter()
_counter_lock = threading.Lock()


def count(name, n=1):
    with _counter_lock:
        COUNTERS[name] += n


def counters_snapshot():
    with _counter_lock:
        return dict(COUNTERS)


_ACQUIRE = """
local waiter = redis.call('get', KEYS[2])
local prio = tonumber(ARGV[3])
if waiter then
  local w = cjson.decode(waiter)
  if tonumber(w.priority) < prio and w.token ~= ARGV[6] then
    return {'yield', waiter}
  end
end
if redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', tonumber(ARGV[2])) then
  if waiter and cjson.decode(waiter).token == ARGV[6] then
    redis.call('del', KEYS[2])
  end
  return {'ok', ''}
end
local holder = redis.call('get', KEYS[1]) or ''
if ARGV[4] == '1' then
  local register = true
  if waiter then
    local w = cjson.decode(waiter)
    register = (w.token == ARGV[6]) or (tonumber(w.priority) > prio)
  end
  if register then
    redis.call('set', KEYS[2], cjson.encode({token=ARGV[6], priority=prio, operation=ARGV[7]}), 'EX', tonumber(ARGV[5]))
  end
end
return {'busy', holder}
"""

_OWNED_DEL = """
local value = redis.call('get', KEYS[1])
if not value then return -1 end
if cjson.decode(value).token == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""

_OWNED_EXPIRE = """
local value = redis.call('get', KEYS[1])
if not value then return -1 end
local data = cjson.decode(value)
if data.token ~= ARGV[1] then return 0 end
data.expires_at = ARGV[3]
redis.call('set', KEYS[1], cjson.encode(data), 'EX', tonumber(ARGV[2]))
return 1
"""

_WAITER_DEL = """
local value = redis.call('get', KEYS[1])
if value and cjson.decode(value).token == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""

_client = None


def _redis():
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
    return _client


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _owner():
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


class Lease:
    """An owned device slot. Release and refresh only act while the stored token is ours."""

    def __init__(self, device_id, operation, token, ttl_seconds, owner, wait_ms):
        self.device_id = device_id
        self.operation = operation
        self.token = token
        self.ttl_seconds = ttl_seconds
        self.owner = owner
        self.wait_ms = wait_ms
        self.released = False

    @property
    def key(self):
        return OPERATION_KEY.format(device_id=self.device_id)

    def refresh(self, ttl_seconds):
        """Owner-only TTL refresh (phase boundaries of long changes). False if no longer ours."""
        expires = _iso(_now() + timedelta(seconds=ttl_seconds))
        try:
            result = _redis().eval(_OWNED_EXPIRE, 1, self.key, self.token, ttl_seconds, expires)
        except redis.RedisError as exc:
            logger.warning("device_lock_refresh_failed device_id=%s operation=%s error=%s",
                           self.device_id, self.operation, type(exc).__name__)
            return False
        if result != 1:
            logger.warning("device_lock_expired_or_missing device_id=%s operation=%s owner=%s reason=refresh",
                           self.device_id, self.operation, self.owner)
            count("lock_lost")
            return False
        self.ttl_seconds = ttl_seconds
        return True

    def release(self):
        if self.released:
            return
        self.released = True
        try:
            result = _redis().eval(_OWNED_DEL, 1, self.key, self.token)
        except redis.RedisError as exc:  # the TTL still frees the slot
            logger.warning("device_lock_release_failed device_id=%s operation=%s error=%s",
                           self.device_id, self.operation, type(exc).__name__)
            return
        if result == 1:
            logger.info("device_lock_released device_id=%s operation=%s owner=%s", self.device_id, self.operation, self.owner)
        else:
            logger.warning("device_lock_expired_or_missing device_id=%s operation=%s owner=%s reason=%s",
                           self.device_id, self.operation, self.owner, "missing" if result == -1 else "taken_over")
            count("lock_lost")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class Acquisition:
    """status: acquired | busy | yield | unavailable. `holder` describes the current slot owner."""

    def __init__(self, status, lease=None, holder=None, wait_ms=0, error=None):
        self.status = status
        self.lease = lease
        self.holder = holder
        self.wait_ms = wait_ms
        self.error = error

    @property
    def acquired(self):
        return self.status == "acquired"

    @property
    def change_active(self):
        """The device is held by (or reserved for) an approved configuration change."""
        return bool(self.holder) and self.holder.get("operation") == "apply"

    @property
    def reason(self):
        if self.status == "unavailable":
            return "coordination_unavailable"
        if self.change_active:
            return "change_in_progress"
        if self.status == "yield":
            return f"higher_priority_waiting:{(self.holder or {}).get('operation')}"
        return f"busy:{(self.holder or {}).get('operation')}"


def _parse(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def acquire_device_operation(device_id, operation_type, *, owner=None, wait_seconds=None, ttl_seconds=None,
                             hostname=None):
    """
    Try to take the device's operation slot.

    Low-priority collectors (wait 0) return immediately with busy/yield. Higher-priority
    operations wait up to `wait_seconds`, registering as the waiter so collectors yield.
    Never raises for Redis errors: returns status "unavailable".
    """
    spec = OPERATIONS[operation_type]
    ttl = int(ttl_seconds or spec["ttl"])
    wait = spec["wait"] if wait_seconds is None else wait_seconds
    owner = owner or _owner()
    token = uuid.uuid4().hex
    started = time.monotonic()
    deadline = started + max(0, wait)
    keys = (OPERATION_KEY.format(device_id=device_id), WAITING_KEY.format(device_id=device_id))
    logged_busy = False

    while True:
        now = _now()
        value = json.dumps({"token": token, "device_id": device_id, "operation": operation_type,
                            "priority": spec["priority"], "exclusive": spec["exclusive"], "owner": owner,
                            "started_at": _iso(now), "expires_at": _iso(now + timedelta(seconds=ttl))})
        will_wait = time.monotonic() < deadline
        try:
            status, raw = _redis().eval(_ACQUIRE, 2, *keys, value, ttl, spec["priority"], "1" if will_wait else "0",
                                        WAITER_TTL_SECONDS, token, operation_type)
        except redis.RedisError as exc:
            count("lock_unavailable")
            logger.warning("device_lock_unavailable device_id=%s host=%s operation=%s error=%s",
                           device_id, hostname, operation_type, type(exc).__name__)
            return Acquisition("unavailable", error=type(exc).__name__)

        wait_ms = int((time.monotonic() - started) * 1000)
        if status == "ok":
            count("lock_acquired")
            logger.info("device_lock_acquired device_id=%s host=%s operation=%s owner=%s wait_ms=%s ttl=%s",
                        device_id, hostname, operation_type, owner, wait_ms, ttl)
            return Acquisition("acquired", lease=Lease(device_id, operation_type, token, ttl, owner, wait_ms),
                               wait_ms=wait_ms)

        holder = _parse(raw)
        if not logged_busy:
            logged_busy = True
            count("lock_contention")
            logger.info("device_lock_busy device_id=%s host=%s operation=%s held_by=%s holder_owner=%s reason=%s",
                        device_id, hostname, operation_type, (holder or {}).get("operation"),
                        (holder or {}).get("owner"), status)
        if status == "yield" or not will_wait:
            if will_wait is False and status == "busy" and wait > 0:
                count("lock_timeout")
            _drop_waiter(device_id, token)
            return Acquisition(status, holder=holder, wait_ms=wait_ms)
        time.sleep(POLL_INTERVAL_SECONDS)


def _drop_waiter(device_id, token):
    try:
        _redis().eval(_WAITER_DEL, 1, WAITING_KEY.format(device_id=device_id), token)
    except redis.RedisError:
        pass  # short TTL


def current_operation(device_id):
    """Current slot holder (dict) or None. Raises redis.RedisError."""
    return _parse(_redis().get(OPERATION_KEY.format(device_id=device_id)))


# ---- change-in-progress state -----------------------------------------------------------
def set_change_in_progress(lease, change_id, job_id, ttl_seconds):
    """Mark an approved change as executing (owned by the apply lease's token)."""
    now = _now()
    value = json.dumps({"token": lease.token, "device_id": lease.device_id, "change_id": change_id, "job_id": job_id,
                        "started_at": _iso(now), "expires_at": _iso(now + timedelta(seconds=ttl_seconds))})
    _redis().set(CHANGE_KEY.format(device_id=lease.device_id), value, ex=int(ttl_seconds))
    logger.info("change_in_progress_set device_id=%s change_id=%s job_id=%s ttl=%s",
                lease.device_id, change_id, job_id, ttl_seconds)


def refresh_change(lease, ttl_seconds):
    """Owner-only refresh of the slot and the change state together (phase boundaries)."""
    expires = _iso(_now() + timedelta(seconds=ttl_seconds))
    ok = lease.refresh(ttl_seconds)
    try:
        _redis().eval(_OWNED_EXPIRE, 1, CHANGE_KEY.format(device_id=lease.device_id), lease.token, ttl_seconds, expires)
    except redis.RedisError:
        pass
    return ok


def clear_change_in_progress(lease):
    """Remove the change state (only ours) and any health grace window it opened."""
    try:
        _redis().eval(_OWNED_DEL, 1, CHANGE_KEY.format(device_id=lease.device_id), lease.token)
        if _redis().delete(GRACE_KEY.format(device_id=lease.device_id)):
            logger.info("change_grace_ended device_id=%s reason=change_finished", lease.device_id)
        logger.info("change_in_progress_cleared device_id=%s", lease.device_id)
    except redis.RedisError as exc:  # both keys expire on their own
        logger.warning("change_in_progress_clear_failed device_id=%s error=%s", lease.device_id, type(exc).__name__)


def change_in_progress(device_id):
    """Change state dict or None. Redis errors -> None (health keeps normal semantics)."""
    try:
        return _parse(_redis().get(CHANGE_KEY.format(device_id=device_id)))
    except redis.RedisError:
        return None


# ---- health grace during a change -----------------------------------------------------------
def grace_started_at(device_id, now_epoch=None):
    """
    Start (if needed) and return the grace window's start (epoch seconds) for a health
    failure seen during an active change. None if Redis is unavailable (no grace).
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    key = GRACE_KEY.format(device_id=device_id)
    try:
        if _redis().set(key, str(now_epoch), nx=True, ex=CHANGE_HEALTH_GRACE_SECONDS * 4):
            count("change_grace")
            logger.info("change_grace_started device_id=%s grace_seconds=%s", device_id, CHANGE_HEALTH_GRACE_SECONDS)
            return now_epoch
        return float(_redis().get(key) or now_epoch)
    except (redis.RedisError, ValueError):
        return None


def end_grace(device_id, reason):
    try:
        if _redis().delete(GRACE_KEY.format(device_id=device_id)):
            logger.info("change_grace_ended device_id=%s reason=%s", device_id, reason)
    except redis.RedisError:
        pass
