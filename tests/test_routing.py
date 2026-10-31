import unittest
from datetime import date
from decimal import Decimal

from finops.models import ModelStats, PriceVersion
from finops.pricing import PriceBook
from finops.routing import recommend_routing

D = date(2026, 10, 20)
BOOK = PriceBook([
    ("gpt-4", PriceVersion(date(2000, 1, 1), Decimal("0.03"), Decimal("0.06"), "gpt-3.5-turbo")),
    ("gpt-3.5-turbo", PriceVersion(date(2000, 1, 1), Decimal("0.0005"), Decimal("0.0015"), None)),
])


def stats(**over):
    base = dict(department_id="support", model_name="gpt-4", requests=10_000,
                prompt_tokens=3_000_000, completion_tokens=1_000_000,  # avg 300 / 100 tokens
                max_p95_total_tokens=600, avg_latency_ms=2000.0)
    base.update(over)
    return ModelStats(**base)


class RoutingTests(unittest.TestCase):
    def test_recommends_downshift_for_short_requests(self):
        recs = recommend_routing([stats(), stats(department_id="x", model_name="gpt-3.5-turbo", avg_latency_ms=500.0)], BOOK, D)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r.department_id, r.current_model, r.suggested_model), ("support", "gpt-4", "gpt-3.5-turbo"))
        self.assertGreater(r.est_monthly_savings, Decimal("100"))
        self.assertGreater(r.savings_pct, 90)
        self.assertEqual(r.confidence, "high")
        self.assertEqual(r.latency_delta_pct, -75.0)  # 500ms vs 2000ms

    def test_skips_long_prompts_and_fat_tails(self):
        self.assertEqual(recommend_routing([stats(prompt_tokens=50_000_000)], BOOK, D), [])
        self.assertEqual(recommend_routing([stats(max_p95_total_tokens=9000)], BOOK, D), [])
        self.assertEqual(recommend_routing([stats(max_p95_total_tokens=None)], BOOK, D), [])

    def test_skips_low_volume_and_models_without_fallback(self):
        self.assertEqual(recommend_routing([stats(requests=50, prompt_tokens=15_000, completion_tokens=5_000)], BOOK, D), [])
        self.assertEqual(recommend_routing([stats(model_name="gpt-3.5-turbo")], BOOK, D), [])

    def test_skips_when_savings_below_threshold(self):
        tiny = stats(requests=300, prompt_tokens=90_000, completion_tokens=30_000)
        self.assertEqual(recommend_routing([tiny], BOOK, D, min_monthly_savings=Decimal("500")), [])


if __name__ == "__main__":
    unittest.main()
