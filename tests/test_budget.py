import unittest
from datetime import date
from decimal import Decimal

from finops.budget import evaluate_budgets, project_month_end

AS_OF = date(2026, 10, 10)  # 10 of 31 days elapsed


def flat(per_day, as_of=AS_OF):
    return {date(as_of.year, as_of.month, d): Decimal(str(per_day)) for d in range(1, as_of.day + 1)}


class BudgetTests(unittest.TestCase):
    def test_projection_flat_usage(self):
        p = project_month_end(flat(10), AS_OF)
        self.assertEqual(p.month_to_date, Decimal("100"))
        self.assertEqual(p.projected_month_end, Decimal("310"))
        self.assertEqual(p.days_remaining, 21)

    def test_projection_tracks_recent_acceleration(self):
        costs = flat(10)
        for d in range(4, 11):  # last 7 days run at 30/day
            costs[date(2026, 10, d)] = Decimal("30")
        p = project_month_end(costs, AS_OF)
        self.assertEqual(p.recent_daily_avg, Decimal("30"))
        self.assertGreater(p.projected_month_end, Decimal("600"))

    def test_critical_when_budget_exceeded(self):
        alerts = evaluate_budgets({"eng": Decimal("90")}, {"eng": flat(10)}, AS_OF)
        self.assertEqual([a.type for a in alerts], ["budget_critical"])
        self.assertEqual(alerts[0].key, "budget_critical:eng:2026-10")

    def test_warning_and_forecast(self):
        alerts = evaluate_budgets({"eng": Decimal("120")}, {"eng": flat(10)}, AS_OF)  # mtd 100 = 83%, proj 310
        self.assertEqual(sorted(a.type for a in alerts), ["budget_forecast", "budget_warning"])

    def test_forecast_only_when_under_warn_threshold(self):
        alerts = evaluate_budgets({"eng": Decimal("200")}, {"eng": flat(10)}, AS_OF)  # mtd 50%, proj 310 > 200
        self.assertEqual([a.type for a in alerts], ["budget_forecast"])

    def test_healthy_budget_is_quiet(self):
        self.assertEqual(evaluate_budgets({"eng": Decimal("1000")}, {"eng": flat(10)}, AS_OF), [])

    def test_no_forecast_in_first_days(self):
        early = date(2026, 10, 2)
        self.assertEqual(evaluate_budgets({"eng": Decimal("10")}, {"eng": flat(1, early)}, early), [])

    def test_zero_budget_ignored(self):
        self.assertEqual(evaluate_budgets({"eng": Decimal("0")}, {"eng": flat(10)}, AS_OF), [])


if __name__ == "__main__":
    unittest.main()
