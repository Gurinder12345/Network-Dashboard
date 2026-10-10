"""
Where in the minute should the 5-minute interface sweep start? A timing simulation with the
REAL coordination code (in-memory Redis) and the production sweep shapes: health every
60 s at :00, telemetry every 60 s at :20 (both concurrency 4, hostname order), topology
every 300 s at :40 (minutes 0, 5, ...), interface every 300 s at minute 2 + offset
(concurrency 4, OS10 first), 10 devices (8 OS6 + 2 OS10 cores).

Per-device durations follow the lab: OS6 health ~5 s (Kenda-HARO-SW-01 observed :04-:09),
telemetry ~6 s (:26-:32), topology ~5 s; OS10 ~13-14 s; interface collection OS6 ~7 s
(live record: 6.8 s), OS10 ~25 s (estimate). Time is compressed (SCALE).

    python -m unittest tests.test_interface_schedule_simulation -v
"""

import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from coordination import device_ops as coord  # noqa: E402
from fakes import fake_redis  # noqa: E402

SCALE = 0.02
DURATIONS = {"dell_os6": {"health": 5, "telemetry": 6, "topology": 5, "interface_poll": 7},
             "dell_os10": {"health": 13, "telemetry": 14, "topology": 14, "interface_poll": 25}}
HOSTS = ["Kenda-Access-user-1", "Kenda-Access-user-2", "Kenda-Core-1", "Kenda-Core-2", "Kenda-HARO-IDF-A",
         "Kenda-HARO-SW-01", "Kenda-HARO-SW-02", "Kenda-HQ-IDF-A", "Kenda-HQ-IDF-B", "Kenda-HQ-SW-01"]
DEVICES = [{"id": i, "hostname": h, "platform": "dell_os10" if "Core" in h else "dell_os6"} for i, h in enumerate(HOSTS, 1)]


class Simulation:
    def __init__(self):
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.active = {}
        self.overlaps = []
        self.interface = {"collected": 0, "skipped": []}
        self.health_bypass = 0
        self.health_wait_ms = []

    def now(self):
        return (time.monotonic() - self.t0) / SCALE

    def ssh(self, device, op):
        with self.lock:
            self.active[device["id"]] = self.active.get(device["id"], 0) + 1
            if self.active[device["id"]] > 1:
                self.overlaps.append((round(self.now(), 1), device["hostname"], op))
        time.sleep(DURATIONS[device["platform"]][op] * SCALE)
        with self.lock:
            self.active[device["id"]] -= 1

    def health(self, device):
        acq = coord.acquire_device_operation(device["id"], "health", hostname=device["hostname"])
        if acq.acquired:
            self.health_wait_ms.append(acq.wait_ms / 1000 / SCALE)
            with acq.lease:
                self.ssh(device, "health")
            return
        with self.lock:
            self.health_bypass += 1  # production health would run unlocked here (concurrent SSH)

    def collector(self, device, op):
        acq = coord.acquire_device_operation(device["id"], op, hostname=device["hostname"])
        if not acq.acquired:
            if op == "interface_poll":
                with self.lock:
                    self.interface["skipped"].append((round(self.now() % 60, 1), device["hostname"], acq.reason))
            return
        with acq.lease:
            self.ssh(device, op)
        if op == "interface_poll":
            with self.lock:
                self.interface["collected"] += 1

    def sweep(self, devices, fn):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fn, devices))


def run(offset_in_minute, minutes=6):
    """Simulate `minutes` minutes; the interface sweep fires once at minute 2 + offset."""
    sim = Simulation()
    events = []
    for minute in range(minutes):
        events += [(minute * 60, "health"), (minute * 60 + 20, "telemetry")]
        if minute % 5 == 0:
            events.append((minute * 60 + 40, "topology"))
        if minute % 5 == 2:
            events.append((minute * 60 + offset_in_minute, "interface"))
    by_hostname = sorted(DEVICES, key=lambda d: d["hostname"])
    os10_first = sorted(DEVICES, key=lambda d: (d["platform"] != "dell_os10", d["hostname"]))
    threads = []
    for at, what in sorted(events):
        delay = at * SCALE - (time.monotonic() - sim.t0)
        if delay > 0:
            time.sleep(delay)
        if what == "health":
            target = lambda: sim.sweep(by_hostname, sim.health)  # noqa: E731
        elif what == "interface":
            target = lambda: sim.sweep(os10_first, lambda d: sim.collector(d, "interface_poll"))  # noqa: E731
        else:
            target = (lambda op: lambda: sim.sweep(by_hostname, lambda d: sim.collector(d, op)))(what)  # noqa: E731
        thread = threading.Thread(target=target)
        thread.start()
        threads.append(thread)
    [t.join() for t in threads]
    return sim


class ScheduleSimulationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        fake_redis.install(self)
        ops = {k: dict(v) for k, v in coord.OPERATIONS.items()}
        for name in ops:
            ops[name]["wait"] = ops[name]["wait"] * SCALE
        mock.patch.object(coord, "OPERATIONS", ops).start()
        mock.patch.object(coord, "POLL_INTERVAL_SECONDS", 0.5 * SCALE).start()

    def test_offsets(self):  # the configured :38 vs the previous :42
        import worker

        chosen_second = worker.INTERFACE_POLL_OFFSET_SECONDS % 60
        self.assertEqual((worker.INTERFACE_POLL_OFFSET_SECONDS // 60) % 5, 2)  # minutes 2, 7, 12, ...
        results = {}
        for second in (chosen_second, 42):
            sim = run(second)
            results[second] = sim
            print(f"\n    interface sweep at minute 2 :{second}: collected {sim.interface['collected']}/10, "
                  f"skipped {len(sim.interface['skipped'])} {sim.interface['skipped']}, health bypasses {sim.health_bypass}, "
                  f"max health wait {max(sim.health_wait_ms):.1f}s, SSH overlaps {sim.overlaps}")
        chosen = results[chosen_second]
        self.assertEqual(chosen.overlaps, [])               # never two SSH operations on one device
        self.assertEqual(chosen.health_bypass, 0)            # health always got the device (it may wait briefly)
        self.assertEqual(chosen.interface["collected"], 10)  # every device collected in this model
        self.assertEqual(chosen.interface["skipped"], [])
        self.assertLess(max(chosen.health_wait_ms), 15)      # within health's own wait budget


if __name__ == "__main__":
    unittest.main()
