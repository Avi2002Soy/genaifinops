import unittest
from datetime import date
from decimal import Decimal

from finops.models import CostLine, ImpactResult
from finops.report import build_report


class FakeRepo:
    def cost_lines(self, since):
        self.since = since
        return [CostLine("support", "gpt-4o", Decimal("12.50"), 100, 50_000)]
    def capture_summary(self, since): return {"parse_rate": 0.9996, "gateway_capture_rate": 0.9991, "gateway_requests_checked": 5000}
    def impact_results(self):
        return [ImpactResult("support", "gpt-4", "gpt-3.5-turbo", date(2026, 10, 1), "measured", 1000, 1000,
                             Decimal("0.05"), Decimal("0.03"), Decimal("20.00"), 40.0, 2000.0, 1300.0, -35.0)]
    def open_opportunities(self): return {"routing_monthly_usd": Decimal("80"), "inefficiency_monthly_usd": Decimal("12")}
    def live_totals(self, day): return [{"department_id": "support", "requests": 40, "cost": Decimal("1.20")}]


class ReportTests(unittest.TestCase):
    def test_report_shape(self):
        repo = FakeRepo()
        r = build_report(repo, date(2026, 10, 31), 30)
        self.assertEqual(repo.since, date(2026, 10, 1))
        self.assertEqual(r["window"], {"from": "2026-10-01", "to": "2026-10-30", "days": 30})
        self.assertEqual(r["showback"]["departments"][0]["department_id"], "support")
        self.assertEqual(r["impact"]["avg_latency_change_pct"], -35.0)
        self.assertEqual(r["impact"]["spend_reduction_pct"], 40.0)
        self.assertEqual(r["today_so_far"][0]["requests"], 40)


if __name__ == "__main__":
    unittest.main()
