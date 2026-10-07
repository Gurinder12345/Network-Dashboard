"""
In-memory stand-in for the Redis commands used by coordination/device_ops.py and
health/cache.py, for tests without a Redis server. The Lua scripts are mirrored in Python
(dispatch on the script text); test_coordination.py runs the same tests against a REAL
Redis when COORD_TEST_REDIS_URL is set, which validates the actual Lua.

TTLs use a controllable clock: advance(seconds) simulates time passing (crash recovery).
`down = True` makes every call raise redis.ConnectionError (Redis unavailable).
"""

import json
import threading
import time

import redis

from coordination import device_ops as coord


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.expiry = {}
        self.offset = 0.0
        self.down = False
        self.lock = threading.RLock()

    # ---- clock / expiry ------------------------------------------------------------------
    def now(self):
        return time.monotonic() + self.offset

    def advance(self, seconds):
        self.offset += seconds

    def _check(self):
        if self.down:
            raise redis.ConnectionError("simulated Redis outage")

    def _alive(self, key):
        expires = self.expiry.get(key)
        if expires is not None and self.now() >= expires:
            self.data.pop(key, None)
            self.expiry.pop(key, None)
        return key in self.data

    def _set(self, key, value, ex=None):
        self.data[key] = str(value)
        if ex is None:
            self.expiry.pop(key, None)
        else:
            self.expiry[key] = self.now() + float(ex)

    # ---- commands --------------------------------------------------------------------------
    def set(self, key, value, nx=False, ex=None):
        with self.lock:
            self._check()
            if nx and self._alive(key):
                return None
            self._set(key, value, ex)
            return True

    def setex(self, key, ttl, value):
        return self.set(key, value, ex=ttl)

    def get(self, key):
        with self.lock:
            self._check()
            return self.data[key] if self._alive(key) else None

    def mget(self, keys):
        return [self.get(k) for k in keys]

    def exists(self, key):
        with self.lock:
            self._check()
            return 1 if self._alive(key) else 0

    def delete(self, *keys):
        with self.lock:
            self._check()
            removed = 0
            for key in keys:
                if self._alive(key):
                    removed += 1
                    self.data.pop(key, None)
                    self.expiry.pop(key, None)
            return removed

    def ttl(self, key):
        with self.lock:
            self._check()
            if not self._alive(key):
                return -2
            expires = self.expiry.get(key)
            return -1 if expires is None else max(0, int(expires - self.now()))

    def eval(self, script, numkeys, *args):
        with self.lock:
            self._check()
            keys, argv = [str(a) for a in args[:numkeys]], [str(a) for a in args[numkeys:]]
            if script == coord._ACQUIRE:
                return self._acquire(keys, argv)
            if script in (coord._OWNED_DEL, coord._WAITER_DEL):
                value = self.data.get(keys[0]) if self._alive(keys[0]) else None
                if value is None:
                    return -1 if script == coord._OWNED_DEL else 0
                if json.loads(value).get("token") == argv[0]:
                    return self.delete(keys[0])
                return 0
            if script == coord._OWNED_EXPIRE:
                if not self._alive(keys[0]):
                    return -1
                data = json.loads(self.data[keys[0]])
                if data.get("token") != argv[0]:
                    return 0
                data["expires_at"] = argv[2]
                self._set(keys[0], json.dumps(data), argv[1])
                return 1
            if "redis.call(\"get\", KEYS[1]) == ARGV[1]" in script:  # FleetLock / API lock release
                if self._alive(keys[0]) and self.data[keys[0]] == argv[0]:
                    return self.delete(keys[0])
                return 0
            raise NotImplementedError(script[:60])

    def _acquire(self, keys, argv):
        value, ttl, prio, will_wait, waiter_ttl, token, operation = argv
        prio = int(prio)
        waiter = self.data[keys[1]] if self._alive(keys[1]) else None
        if waiter:
            w = json.loads(waiter)
            if int(w["priority"]) < prio and w["token"] != token:
                return ["yield", waiter]
        if not self._alive(keys[0]):
            self._set(keys[0], value, ttl)
            if waiter and json.loads(waiter)["token"] == token:
                self.delete(keys[1])
            return ["ok", ""]
        holder = self.data[keys[0]]
        if will_wait == "1":
            register = True
            if waiter:
                w = json.loads(waiter)
                register = w["token"] == token or int(w["priority"]) > prio
            if register:
                self._set(keys[1], json.dumps({"token": token, "priority": prio, "operation": operation}), waiter_ttl)
        return ["busy", holder]


def install(testcase, client=None):
    """Point coordination (and the health cache) at a fake (or given) Redis for one test."""
    from unittest import mock

    from health import cache

    fake = client or FakeRedis()
    testcase.addCleanup(mock.patch.stopall)
    mock.patch.object(coord, "_redis", return_value=fake).start()
    mock.patch.object(cache, "_redis", return_value=fake).start()
    return fake
