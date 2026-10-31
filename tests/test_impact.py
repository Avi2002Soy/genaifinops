import unittest
from datetime import date
from decimal import Decimal

from finops.config import Settings
from finops.impact import impact_windows, measure_impact, run_impact_review, summarize_impact
from finops.models import AcceptedRecommendation, WindowTotals

REC = AcceptedRecommendation("support", "gpt-4", "gpt-3.5-turbo", date(2026, 10, 10))


class WindowTests(unittest.TestCase):
    def test_windows(self):
        b0, b1, a0, a1 = impact_windows(date(2026, 10, 10), date(2026, 10, 30), 14, 7)
        self.assertEqual((b0, b1), (date(2026, 9, 26), date(2026, 10, 10)))
        self.assertEqual((a0, a1), (date(2026, 10, 11), date(2026, 10, 25)))

    def test_after_window_is_cut_at_last_complete_day(self):
        _, _, a0, a1 = impact_windows(date(2026, 10, 20), date(2026, 10, 30), 14, 7)
        self.assertEqual((a0, a1), (date(2026, 10, 21), date(2026, 10, 31)))

    def test_too_early_returns_none(self):
        self.assertIsNone(impact_windows(date(2026, 10, 28), date(2026, 10, 30), 14, 7))


class MeasureTests(unittest.TestCase):
    def test_cost_and_latency_change(self):
        before = WindowTotals(10_000, Decimal("500.00"), 2000.0)   # $0.05 / request
        after = WindowTotals(8_000, Decimal("144.00"), 1300.0)     # $0.018 / request
        r = measure_impact(REC, before, after)
        self.assertEqual(r.status, "measured")
        self.assertEqual(r.cost_reduction_pct, 64.0)
        self.assertEqual(r.avoided_cost, Decimal("256.00"))        # (0.05-0.018) * 8000
        self.assertEqual(r.latency_change_pct, -35.0)

    def test_rates_not_totals(self):
        # Traffic halved but unit cost unchanged: no saving may be claimed from the drop in volume.
        r = measure_impact(REC, WindowTotals(10_000, Decimal("100"), None), WindowTotals(5_000, Decimal("50"), None))
        self.assertEqual((r.cost_reduction_pct, r.avoided_cost), (0.0, Decimal("0.00")))
        self.assertIsNone(r.latency_change_pct)

    def test_insufficient_data_reports_no_percentages(self):
        r = measure_impact(REC, WindowTotals(10_000, Decimal("500"), 2000.0), WindowTotals(40, Decimal("1"), 900.0))
        self.assertEqual(r.status, "insufficient_data")
        self.assertIsNone(r.cost_reduction_pct)
        self.assertIsNone(r.latency_change_pct)

    def test_cost_increase_is_negative_saving(self):
        r = measure_impact(REC, WindowTotals(1000, Decimal("10"), None), WindowTotals(1000, Decimal("15"), None))
        self.assertEqual(r.cost_reduction_pct, -50.0)
        self.assertEqual(r.avoided_cost, Decimal("-5.00"))

    def test_summary_is_request_weighted(self):
        a = measure_impact(REC, WindowTotals(1000, Decimal("100"), 2000.0), WindowTotals(1000, Decimal("60"), 1000.0))
        b = measure_impact(REC, WindowTotals(1000, Decimal("100"), 1000.0), WindowTotals(3000, Decimal("240"), 1000.0))
        c = measure_impact(REC, WindowTotals(5, Decimal("1"), None), WindowTotals(5, Decimal("1"), None))
        s = summarize_impact([a, b, c])
        self.assertEqual((s["recommendations_measured"], s["recommendations_insufficient_data"]), (2, 1))
        # a avoided 40, b avoided 60 -> 100 avoided of (60+240+100)=400 baseline
        self.assertEqual(s["avoided_cost_usd"], Decimal("100.00"))
        self.assertEqual(s["spend_reduction_pct"], 25.0)
        # latency: a -50% weight 1000, b 0% weight 3000 -> -12.5
        self.assertEqual(s["avg_latency_change_pct"], -12.5)

    def test_empty_summary(self):
        s = summarize_impact([])
        self.assertEqual((s["spend_reduction_pct"], s["avg_latency_change_pct"]), (None, None))


class FakeRepo:
    def __init__(self, recs): self.recs, self.saved, self.runs = recs, [], []
    def accepted_recommendations(self): return self.recs
    def window_totals(self, dept, start, end):
        return WindowTotals(5000, Decimal("250"), 2000.0) if end <= date(2026, 10, 10) else WindowTotals(5000, Decimal("100"), 1300.0)
    def save_impact(self, results): self.saved = list(results)
    def record_run(self, s, started): self.runs.append(s)


class FakeDb:
    def __init__(self, repo): self.repo = repo
    def run(self, work): return work(self.repo)


class RunTests(unittest.TestCase):
    def test_skips_recommendations_that_are_too_recent(self):
        recent = AcceptedRecommendation("sales", "gpt-4o", "gpt-4o-mini", date(2026, 10, 29))
        repo = FakeRepo([REC, recent])
        s = run_impact_review(Settings("w", "c"), FakeDb(repo), today=date(2026, 10, 31))
        self.assertEqual([r.department_id for r in repo.saved], ["support"])
        self.assertEqual(s.recommendations, 1)
        self.assertEqual(repo.runs[0].run_type, "impact")


if __name__ == "__main__":
    unittest.main()
