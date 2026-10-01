"""
Staggered Beat schedules: health :00, telemetry :20 (every minute), topology :40 (every
5 minutes). Drives the real schedule objects from worker.py with a simulated clock the
same way Beat does (is_due -> run -> last_run_at = now). Run inside the worker image:
    python -m unittest tests.test_beat_schedule -v
"""

import os
import pickle
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker  # noqa: E402
from scheduling import offset_schedule  # noqa: E402

UTC = timezone.utc
ENTRIES = {
    "fleet-health-every-60s": ("network_worker.health_check_all_devices", 60, 0, 55),
    "fleet-metrics-every-60s": ("network_worker.collect_fleet_metrics", 60, 20, 55),
    "topology-discovery-every-5m": ("network_worker.discover_topology_all_devices", 300, 40, 280),
}
# Filesystem/DB-only housekeeping (no SSH): checked for same-second starts, not for spacing.
HOUSEKEEPING = {"pcap-cleanup-hourly": ("network_worker.cleanup_pcap_files", 3600, 50, 3000)}
SSH_SWEEPS = set(ENTRIES)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


def simulate(start, minutes, step=0.25):
    """Fire times per entry, using copies of the real schedules on a fake clock."""
    clock = Clock(start)
    entries = {
        name: (offset_schedule(entry["schedule"].period, entry["schedule"].offset, nowfun=clock), start)
        for name, entry in worker.app.conf.beat_schedule.items()
    }
    fired = {name: [] for name in entries}
    end = start + timedelta(minutes=minutes)
    while clock.now < end:
        for name, (sched, last_run) in list(entries.items()):
            if sched.is_due(last_run).is_due:
                fired[name].append(clock.now)
                entries[name] = (sched, clock.now)
        clock.now += timedelta(seconds=step)
    return fired


class ScheduleDefinitionTests(unittest.TestCase):
    def test_entries_tasks_cadence_and_expiry(self):
        schedule = worker.app.conf.beat_schedule
        self.assertEqual(set(schedule), set(ENTRIES) | set(HOUSEKEEPING))
        for name, (task, period, offset, expires) in {**ENTRIES, **HOUSEKEEPING}.items():
            with self.subTest(name=name):
                entry = schedule[name]
                self.assertEqual(entry["task"], task)
                self.assertIn(task, worker.app.tasks)
                self.assertIsInstance(entry["schedule"], offset_schedule)
                self.assertEqual((entry["schedule"].period, entry["schedule"].offset), (period, offset))
                self.assertEqual(entry["options"], {"expires": expires})  # unchanged
                if name in ENTRIES:
                    self.assertEqual(entry["kwargs"], {"trigger": "schedule"})

    def test_offsets_leave_room_before_the_next_sweep(self):
        offsets = sorted(worker.app.conf.beat_schedule[n]["schedule"].offset for n in ENTRIES)
        self.assertEqual(offsets, [0, 20, 40])


class TimelineTests(unittest.TestCase):
    # Arbitrary, unaligned Beat start time.
    START = datetime(2026, 10, 1, 11, 58, 7, 300000, tzinfo=UTC)  # spans the 12:00 hourly cleanup

    @classmethod
    def setUpClass(cls):
        cls.fired = simulate(cls.START, minutes=16)

    def test_health_every_minute_at_second_0(self):
        times = self.fired["fleet-health-every-60s"]
        self.assertGreaterEqual(len(times), 15)
        self.assertTrue(all(t.second == 0 for t in times))
        self.assertTrue(all((b - a) == timedelta(seconds=60) for a, b in zip(times, times[1:])))

    def test_metrics_every_minute_at_second_20(self):
        times = self.fired["fleet-metrics-every-60s"]
        self.assertGreaterEqual(len(times), 15)
        self.assertTrue(all(t.second == 20 for t in times))
        self.assertTrue(all((b - a) == timedelta(seconds=60) for a, b in zip(times, times[1:])))

    def test_topology_every_five_minutes_at_second_40(self):
        times = self.fired["topology-discovery-every-5m"]
        self.assertGreaterEqual(len(times), 3)
        self.assertTrue(all(t.second == 40 and t.minute % 5 == 0 for t in times))
        self.assertTrue(all((b - a) == timedelta(seconds=300) for a, b in zip(times, times[1:])))

    def test_no_two_sweeps_start_in_the_same_second(self):
        everything = [t.replace(microsecond=0) for times in self.fired.values() for t in times]
        self.assertEqual(len(everything), len(set(everything)))  # including housekeeping
        seconds = [t.replace(microsecond=0) for name, times in self.fired.items() if name in SSH_SWEEPS for t in times]
        # At least 20 s between any two SSH sweeps.
        ordered = sorted(seconds)
        self.assertGreaterEqual(min(b - a for a, b in zip(ordered, ordered[1:])), timedelta(seconds=20))

    def test_first_run_after_start_is_the_next_aligned_slot(self):
        # Within one simulated clock tick (0.25 s) after the slot, as Beat would see it.
        expected = {
            "fleet-health-every-60s": datetime(2026, 10, 1, 11, 59, 0, tzinfo=UTC),
            "fleet-metrics-every-60s": datetime(2026, 10, 1, 11, 58, 20, tzinfo=UTC),
            "topology-discovery-every-5m": datetime(2026, 10, 1, 12, 0, 40, tzinfo=UTC),
            "pcap-cleanup-hourly": datetime(2026, 10, 1, 12, 0, 50, tzinfo=UTC),
        }
        for name, slot in expected.items():
            first = self.fired[name][0]
            self.assertTrue(timedelta(0) <= first - slot < timedelta(seconds=0.25), (name, first))


class OffsetScheduleTests(unittest.TestCase):
    def test_missed_slots_fire_once_then_realign(self):
        clock = Clock(datetime(2026, 10, 1, 12, 10, 5, tzinfo=UTC))
        sched = offset_schedule(60, 20, nowfun=clock)
        last_run = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)  # Beat was down for 10 minutes
        state = sched.is_due(last_run)
        self.assertTrue(state.is_due)
        self.assertAlmostEqual(state.next, 15, places=3)       # next check at 12:10:20
        self.assertFalse(sched.is_due(clock.now).is_due)        # ran at 12:10:05; not again until :20

    def test_not_due_reports_seconds_until_slot(self):
        clock = Clock(datetime(2026, 10, 1, 12, 0, 50, tzinfo=UTC))
        state = offset_schedule(60, 20, nowfun=clock).is_due(datetime(2026, 10, 1, 12, 0, 20, 10, tzinfo=UTC))
        self.assertFalse(state.is_due)
        self.assertAlmostEqual(state.next, 30, places=3)

    def test_remaining_estimate(self):
        clock = Clock(datetime(2026, 10, 1, 12, 2, 0, tzinfo=UTC))
        sched = offset_schedule(300, 40, nowfun=clock)
        self.assertEqual(sched.remaining_estimate(clock.now), timedelta(minutes=3, seconds=40))

    def test_invalid_arguments(self):
        for period, offset in ((0, 0), (60, 60), (60, -1)):
            with self.subTest(period=period, offset=offset):
                with self.assertRaises(ValueError):
                    offset_schedule(period, offset)

    def test_pickles_for_beat_schedule_file(self):
        for entry in worker.app.conf.beat_schedule.values():
            restored = pickle.loads(pickle.dumps(entry["schedule"]))
            self.assertEqual(restored, entry["schedule"])


def timeline(minutes=10):
    start = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC) - timedelta(milliseconds=1)
    fired = simulate(start, minutes, step=0.5)
    short = {"fleet-health-every-60s": "health", "fleet-metrics-every-60s": "metrics", "topology-discovery-every-5m": "topology",
             "pcap-cleanup-hourly": "pcap-cleanup"}
    events = sorted((t, short[name]) for name, times in fired.items() for t in times)
    return [f"{(t - start - timedelta(milliseconds=-1)).seconds // 60:02d}:{t.second:02d} {label}" for t, label in events]


if __name__ == "__main__":
    if "--timeline" in sys.argv:
        print("\n".join(timeline()))
    else:
        unittest.main()
