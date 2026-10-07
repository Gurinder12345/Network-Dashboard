"""
Load simulation of device coordination (no switches, no SSH): 10 devices (8 OS6, 2 OS10
cores) on the production schedule -- health every 60 s at :00, telemetry every 60 s at
:20, topology every 5 min at :40, 4 concurrent device operations per sweep, a sweep that
is still running when its next slot comes is dropped (Beat `expires` + fleet lock) -- and
one approved configuration change on Kenda-Core-1 at t=70 s lasting 90 s.

Durations (seconds) follow the measured lab numbers (OS6 telemetry ~5.7 s, OS10 ~13.8 s;
OS10 SSH login ~13 s). Time is compressed (SCALE) so 6 simulated minutes run in ~7 s.
The real coordination code is used (in-memory Redis; real Redis if COORD_TEST_REDIS_URL).

Prints an event timeline:
    python -m unittest tests.test_coordination_simulation -v
"""

import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import redis

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from coordination import device_ops as coord  # noqa: E402
from fakes import fake_redis  # noqa: E402

SCALE = 0.02  # 1 simulated second = 20 ms
SIM_SECONDS = 360
CHANGE_AT, CHANGE_LENGTH = 70, 90
DURATIONS = {"dell_os6": {"health": 2.0, "telemetry": 5.7, "topology": 4.0},
             "dell_os10": {"health": 13.0, "telemetry": 13.8, "topology": 14.0}}
DEVICES = ([{"id": i, "hostname": f"Kenda-OS6-{i}", "platform": "dell_os6"} for i in range(1, 9)]
           + [{"id": 11, "hostname": "Kenda-Core-1", "platform": "dell_os10"},
              {"id": 12, "hostname": "Kenda-Core-2", "platform": "dell_os10"}])
CORE1 = 11


class Simulation:
    def __init__(self):
        self.t0 = time.monotonic()
        self.events = []
        self.lock = threading.Lock()
        self.active = {}           # device -> number of SSH sessions now
        self.overlaps = []
        self.health = {d["id"]: "healthy" for d in DEVICES}
        self.running = {}          # sweep name -> running flag (fleet lock)
        self.sweeps = {"started": 0, "dropped": 0}

    def now(self):
        return (time.monotonic() - self.t0) / SCALE

    def log(self, device, op, event):
        with self.lock:
            self.events.append((round(self.now(), 1), device["hostname"], op, event))

    def ssh(self, device, op):
        with self.lock:
            self.active[device["id"]] = self.active.get(device["id"], 0) + 1
            if self.active[device["id"]] > 1:
                self.overlaps.append((round(self.now(), 1), device["hostname"], op))
        time.sleep(DURATIONS[device["platform"]][op] * SCALE)
        with self.lock:
            self.active[device["id"]] -= 1

    # ---- the production decision logic, per operation ---------------------------------------
    def health_check(self, device):
        acq = coord.acquire_device_operation(device["id"], "health", hostname=device["hostname"])
        if acq.acquired:
            with acq.lease:
                self.ssh(device, "health")
            self.log(device, "health", "ok")
            return
        if acq.status != "unavailable" and (acq.holder or {}).get("priority", 9) <= 1:
            self.log(device, "health", "tcp_only (change workflow owns device; status kept)")
            return
        self.ssh(device, "health")  # bypass a slow collector: health never disappears
        self.log(device, "health", f"bypass:{(acq.holder or {}).get('operation')}")

    def collector(self, device, op):
        acq = coord.acquire_device_operation(device["id"], op, hostname=device["hostname"])
        if not acq.acquired:
            self.log(device, op, "skipped_change" if acq.change_active else "skipped_busy")
            return
        with acq.lease:
            self.ssh(device, op)
        self.log(device, op, "success")

    def sweep(self, name, fn):
        with self.lock:
            if self.running.get(name):
                self.sweeps["dropped"] += 1
                self.events.append((round(self.now(), 1), "fleet", name, "sweep dropped (previous still running)"))
                return
            self.running[name] = True
            self.sweeps["started"] += 1
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(fn, DEVICES))
        finally:
            with self.lock:
                self.running[name] = False

    def change(self):
        device = next(d for d in DEVICES if d["id"] == CORE1)
        acq = coord.acquire_device_operation(CORE1, "apply", owner="change:sim-184", hostname=device["hostname"])
        assert acq.acquired, acq.status
        coord.set_change_in_progress(acq.lease, "sim-184", "job-sim", 600)
        self.log(device, "apply", f"change sim-184 started (waited {acq.wait_ms / 1000 / SCALE:.1f}s sim)")
        try:
            with self.lock:
                self.active[CORE1] = self.active.get(CORE1, 0) + 1
                if self.active[CORE1] > 1:
                    self.overlaps.append((round(self.now(), 1), device["hostname"], "apply"))
            for phase, seconds in (("pre-apply backup", 15), ("apply", 40), ("postcheck", 22), ("post-change health", 13)):
                self.log(device, "apply", phase)
                time.sleep(seconds * SCALE)
            with self.lock:
                self.active[CORE1] -= 1
        finally:
            coord.clear_change_in_progress(acq.lease)
            acq.lease.release()
        self.log(device, "apply", "change finished; coordination released")


class LoadSimulationTests(unittest.TestCase):
    real = False

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        if os.getenv("COORD_TEST_REDIS_URL") and self.real:
            client = redis.Redis.from_url(os.getenv("COORD_TEST_REDIS_URL"), decode_responses=True)
            client.flushdb()
            fake_redis.install(self, client)
        else:
            fake_redis.install(self)
        ops = {k: dict(v) for k, v in coord.OPERATIONS.items()}
        for name in ops:  # production waits, compressed to simulated time
            ops[name]["wait"] = ops[name]["wait"] * SCALE
        mock.patch.object(coord, "OPERATIONS", ops).start()
        mock.patch.object(coord, "POLL_INTERVAL_SECONDS", 0.5 * SCALE).start()

    def test_fleet_with_one_change(self):
        sim = Simulation()
        threads = []
        schedule = sorted([(t, "health") for t in range(0, SIM_SECONDS, 60)] + [(t, "telemetry") for t in range(20, SIM_SECONDS, 60)]
                          + [(t, "topology") for t in range(40, SIM_SECONDS, 300)] + [(CHANGE_AT, "change")])
        for at, what in schedule:
            delay = at * SCALE - (time.monotonic() - sim.t0)
            if delay > 0:
                time.sleep(delay)
            if what == "change":
                target = sim.change
            elif what == "health":
                target = lambda: sim.sweep("health", sim.health_check)  # noqa: E731
            else:
                target = (lambda op: lambda: sim.sweep(op, lambda d: sim.collector(d, op)))(what)  # noqa: E731
            thread = threading.Thread(target=target)
            thread.start()
            threads.append(thread)
        [t.join() for t in threads]

        events = sorted(sim.events)
        # The change waits for an in-flight Core-1 operation to finish (no preemption), so the
        # window is measured from its real start to its release.
        start = next(e[0] for e in events if e[3].startswith("change sim-184 started"))
        end = next(e[0] for e in events if e[3].startswith("change finished"))
        self.assertLess(start - CHANGE_AT, 20)  # waited only for the current holder
        change_window = [e for e in events if start < e[0] < end]
        core1 = [e for e in change_window if e[1] == "Kenda-Core-1" and e[2] != "apply"]
        others_ok = [e for e in change_window if e[1] not in ("Kenda-Core-1", "fleet") and e[3] in ("ok", "success")]

        print("\n    t(s)   device          operation  event")
        shown = [e for e in events if e[1] in ("Kenda-Core-1", "Kenda-Core-2", "fleet") and 55 <= e[0] <= 200]
        for t, device, op, event in shown:
            print(f"    {t:6.1f} {device:15} {op:10} {event}")
        counts = {}
        for _, device, op, event in events:
            key = (op, event.split(" ")[0])
            counts[key] = counts.get(key, 0) + 1
        print("    totals:", dict(sorted(counts.items())))
        print("    sweeps:", sim.sweeps, "overlaps:", sim.overlaps)

        self.assertEqual(sim.overlaps, [])  # no two SSH operations ever overlapped on one device
        self.assertTrue(core1)  # Core-1 collectors/health saw the change ...
        self.assertTrue(all(e[3].startswith(("skipped_change", "tcp_only")) for e in core1), core1)
        self.assertTrue(any(e[3] == "skipped_change" for e in core1))
        self.assertGreater(len(others_ok), 20)  # ... while the rest of the fleet kept working
        self.assertTrue(any(e[1] == "Kenda-Core-2" and e[3] in ("ok", "success") for e in change_window))
        self.assertFalse(any("bypass" in e[3] for e in events))  # health never had to fight a collector
        after = [e for e in events if e[1] == "Kenda-Core-1" and e[0] > end and e[2] != "apply"]
        self.assertTrue(any(e[3] in ("ok", "success") for e in after))  # Core-1 polling resumed
        expected = len(range(0, SIM_SECONDS, 60)) + len(range(20, SIM_SECONDS, 60)) + len(range(40, SIM_SECONDS, 300))
        self.assertEqual(sim.sweeps["started"] + sim.sweeps["dropped"], expected)  # no backlog replay
        self.assertEqual(set(sim.health.values()), {"healthy"})  # skips never degraded anything


@unittest.skipUnless(os.getenv("COORD_TEST_REDIS_URL"), "COORD_TEST_REDIS_URL not set (disposable Redis)")
class LoadSimulationRealRedisTests(LoadSimulationTests):
    real = True


if __name__ == "__main__":
    unittest.main()
