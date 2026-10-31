import unittest
from datetime import date
from decimal import Decimal

from finops.efficiency import detect_inefficiencies
from finops.models import ModelStats, PriceVersion
from finops.pricing import PriceBook

BOOK = PriceBook([("gpt-4", PriceVersion(date(2000, 1, 1), Decimal("0.03"), Decimal("0.06")))])
AS_OF = date(2026, 10, 30)


def stats(req=1000, p=4000, c=200, bad=0, model="gpt-4"):
    return ModelStats("eng", model, req, req * p, req * c, 6000, 1500.0, bad)


class EfficiencyTests(unittest.TestCase):
    def kinds(self, s, **kw):
        return {i.kind: i for i in detect_inefficiencies([s], BOOK, AS_OF, **kw)}

    def test_prompt_bloat_is_costed(self):
        i = self.kinds(stats())["prompt_bloat"]
        # (4000-1500) tokens * 1000 req / 1000 * $0.03 = $75 over a 30-day window
        self.assertEqual(i.est_monthly_waste, Decimal("75.00"))
        self.assertEqual(i.severity, "warning")

    def test_window_scaling(self):
        i = detect_inefficiencies([stats()], BOOK, AS_OF, window_days=15)[0]
        self.assertEqual(i.est_monthly_waste, Decimal("150.00"))

    def test_short_prompts_and_balanced_ratio_not_flagged(self):
        self.assertEqual(self.kinds(stats(p=900, c=400)), {})
        self.assertNotIn("prompt_bloat", self.kinds(stats(p=4000, c=1000)))  # ratio 4:1 < 10:1

    def test_uncapped_completion(self):
        i = self.kinds(stats(p=500, c=2000))["uncapped_completion"]
        # completion cost = 1000*2000/1000*0.06 = $120; 20% = $24
        self.assertEqual(i.est_monthly_waste, Decimal("24.00"))

    def test_low_volume_ignored_but_telemetry_gap_is_not(self):
        s = stats(req=50, bad=20)
        found = self.kinds(s)
        self.assertEqual(set(found), {"telemetry_gap"})

    def test_telemetry_gap_threshold(self):
        self.assertNotIn("telemetry_gap", self.kinds(stats(req=5000, bad=10)))   # 0.2% < 1%
        self.assertIn("telemetry_gap", self.kinds(stats(req=1000, bad=50)))

    def test_unknown_model_cannot_be_costed(self):
        self.assertEqual(self.kinds(stats(model="mystery")), {})

    def test_sorted_by_waste(self):
        found = detect_inefficiencies([stats(), stats(req=300)], BOOK, AS_OF)
        wastes = [i.est_monthly_waste for i in found]
        self.assertEqual(wastes, sorted(wastes, reverse=True))


if __name__ == "__main__":
    unittest.main()
