"""
Counter-delta utilization, error deltas and status semantics (pure functions).
    python -m unittest tests.test_interface_utilization -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interfaces.utilization import compute, status_of, utilization_pct  # noqa: E402

T1 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
T2 = T1 + timedelta(seconds=300)
GIG = 1_000_000_000
MAX_GAP = 900


def prev(**kw):
    return {"collected_at": T1, "rx_bytes": 1_000_000, "tx_bytes": 2_000_000, "rx_errors": 0, "tx_errors": 0,
            "crc_errors": 0, "input_discards": 0, "output_discards": 0, **kw}


def cur(**kw):
    return {"speed_bps": GIG, "rx_bytes": 1_000_000, "tx_bytes": 2_000_000, "rx_errors": 0, "tx_errors": 0,
            "crc_errors": 0, "input_discards": 0, "output_discards": 0, **kw}


class UtilizationTests(unittest.TestCase):
    def test_normal_delta(self):
        # 3.75 GB in 300 s = 100 Mb/s = 10 % of 1 Gb/s
        result = compute(prev(), cur(rx_bytes=1_000_000 + 3_750_000_000, tx_bytes=2_000_000 + 375_000_000), T2, MAX_GAP)
        self.assertEqual(result["rx_utilization_pct"], 10.0)
        self.assertEqual(result["tx_utilization_pct"], 1.0)
        self.assertFalse(result["erroring"])

    def test_first_sample_is_null_not_zero(self):
        result = compute(None, cur(), T2, MAX_GAP)
        self.assertIsNone(result["rx_utilization_pct"])
        self.assertIsNone(result["tx_utilization_pct"])
        self.assertIsNone(result["errors_delta"])

    def test_idle_link_is_a_real_zero(self):
        self.assertEqual(compute(prev(), cur(), T2, MAX_GAP)["rx_utilization_pct"], 0.0)

    def test_counter_reset_reboot_and_negative_delta(self):
        for after_reset in (0, 500, 999_999):  # counters cleared / device rebooted / smaller value
            self.assertIsNone(compute(prev(), cur(rx_bytes=after_reset), T2, MAX_GAP)["rx_utilization_pct"])

    def test_counter_wrap_is_not_guessed(self):
        # A 32-bit wrap and a reset look identical without more data: no value, no spike.
        self.assertIsNone(compute(prev(rx_bytes=2**32 - 1000), cur(rx_bytes=5000), T2, MAX_GAP)["rx_utilization_pct"])

    def test_unknown_and_zero_speed(self):
        self.assertIsNone(compute(prev(), cur(speed_bps=None, rx_bytes=10**9), T2, MAX_GAP)["rx_utilization_pct"])
        self.assertIsNone(compute(prev(), cur(speed_bps=0, rx_bytes=10**9), T2, MAX_GAP)["rx_utilization_pct"])
        self.assertIsNone(utilization_pct(0, 100, 300, -5))

    def test_elapsed_zero_negative_and_invalid(self):
        self.assertIsNone(compute(prev(), cur(rx_bytes=10**9), T1, MAX_GAP)["rx_utilization_pct"])
        self.assertIsNone(compute(prev(), cur(rx_bytes=10**9), T1 - timedelta(seconds=5), MAX_GAP)["rx_utilization_pct"])
        self.assertIsNone(compute(prev(collected_at="2026-10-09"), cur(rx_bytes=10**9), T2, MAX_GAP)["rx_utilization_pct"])

    def test_long_polling_gap(self):
        late = T1 + timedelta(seconds=MAX_GAP + 1)
        self.assertIsNone(compute(prev(), cur(rx_bytes=10**9), late, MAX_GAP)["rx_utilization_pct"])
        one_missed = T1 + timedelta(seconds=600)
        self.assertIsNotNone(compute(prev(), cur(rx_bytes=10**9), one_missed, MAX_GAP)["rx_utilization_pct"])

    def test_speed_change_or_implausible_rate(self):
        # 1 Gb/s link renegotiated to 100 Mb/s: a delta far above line rate is rejected.
        self.assertIsNone(compute(prev(), cur(speed_bps=100_000_000, rx_bytes=1_000_000 + 37_500_000_000), T2, MAX_GAP)["rx_utilization_pct"])
        # Slight overshoot from CLI read timing is clamped to 100.
        self.assertEqual(utilization_pct(0, int(GIG * 300 / 8 * 1.03), 300, GIG), 100.0)

    def test_missing_counters(self):
        self.assertIsNone(compute(prev(), cur(rx_bytes=None), T2, MAX_GAP)["rx_utilization_pct"])
        self.assertIsNone(compute(prev(rx_bytes=None), cur(), T2, MAX_GAP)["rx_utilization_pct"])


class ErrorDeltaTests(unittest.TestCase):
    def test_erroring_needs_an_increase(self):
        lifetime = compute(prev(crc_errors=5000), cur(crc_errors=5000), T2, MAX_GAP)
        self.assertFalse(lifetime["erroring"])  # non-zero lifetime counter alone is not "erroring"
        self.assertEqual(lifetime["crc_delta"], 0)
        rising = compute(prev(crc_errors=5000), cur(crc_errors=5007), T2, MAX_GAP)
        self.assertTrue(rising["erroring"])
        self.assertEqual(rising["crc_delta"], 7)
        rx = compute(prev(), cur(rx_errors=3), T2, MAX_GAP)
        self.assertTrue(rx["erroring"])
        self.assertEqual(rx["errors_delta"], 3)

    def test_discards_are_reported_but_not_erroring(self):
        result = compute(prev(), cur(output_discards=40), T2, MAX_GAP)
        self.assertEqual(result["discards_delta"], 40)
        self.assertFalse(result["erroring"])

    def test_one_counter_reset_makes_the_combined_delta_unknown(self):
        result = compute(prev(rx_errors=10, tx_errors=0), cur(rx_errors=2, tx_errors=0), T2, MAX_GAP)
        self.assertIsNone(result["errors_delta"])  # not 0: errors may have been hidden by the reset

    def test_reset_never_flags_erroring(self):
        self.assertFalse(compute(prev(crc_errors=50), cur(crc_errors=2), T2, MAX_GAP)["erroring"])
        self.assertFalse(compute(None, cur(crc_errors=99), T2, MAX_GAP)["erroring"])


class StatusTests(unittest.TestCase):
    def test_semantics(self):
        self.assertEqual(status_of("up", "up"), "up")
        self.assertEqual(status_of(None, "up"), "up")  # a link cannot be up while shut down
        self.assertEqual(status_of("up", "down"), "down")
        self.assertEqual(status_of("down", "down"), "admin_down")
        self.assertEqual(status_of("down", None), "admin_down")
        self.assertEqual(status_of(None, "down"), "unknown")  # never guessed as down
        self.assertEqual(status_of(None, None), "unknown")


if __name__ == "__main__":
    unittest.main()
