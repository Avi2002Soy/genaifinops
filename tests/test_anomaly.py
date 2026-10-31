import unittest
from datetime import date, timedelta
from decimal import Decimal

from finops.anomaly import detect_anomalies, robust_score

TARGET = date(2026, 10, 20)


def series(values, end=TARGET):
    """values[-1] is the target day, earlier values go backwards in time."""
    n = len(values)
    return {end - timedelta(days=n - 1 - i): Decimal(str(v)) for i, v in enumerate(values)}


class AnomalyTests(unittest.TestCase):
    def test_flags_clear_spike(self):
        hist = [10, 11, 9, 10, 12, 10, 11, 9, 10, 11]
        found = detect_anomalies({"eng": series(hist + [60])}, TARGET)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].department_id, "eng")
        self.assertGreater(found[0].ratio, 4)

    def test_normal_noise_not_flagged(self):
        hist = [10, 11, 9, 10, 12, 10, 11, 9, 10, 11]
        self.assertEqual(detect_anomalies({"eng": series(hist + [12])}, TARGET), [])

    def test_flat_history_uses_fallback_and_min_delta(self):
        flat = [5] * 10
        self.assertEqual(len(detect_anomalies({"d": series(flat + [20])}, TARGET)), 1)
        # tiny absolute change on a flat history is ignored (below $1 delta)
        self.assertEqual(detect_anomalies({"d": series(flat + [5.5])}, TARGET), [])

    def test_insufficient_history_skipped(self):
        self.assertEqual(detect_anomalies({"new": series([1, 1, 1, 100])}, TARGET), [])

    def test_drops_are_not_flagged(self):
        hist = [10, 11, 9, 10, 12, 10, 11, 9, 10, 11]
        self.assertEqual(detect_anomalies({"eng": series(hist + [0])}, TARGET), [])

    def test_one_old_spike_does_not_mask_a_new_one(self):
        hist = [10, 10, 10, 200, 10, 10, 10, 10, 10, 10]  # mean/stdev would be inflated by the 200
        self.assertEqual(len(detect_anomalies({"eng": series(hist + [80])}, TARGET)), 1)

    def test_robust_score_zero_for_median(self):
        self.assertEqual(robust_score(10, [9, 10, 11, 10, 10]), 0.0)


if __name__ == "__main__":
    unittest.main()
