import unittest
from datetime import date
from decimal import Decimal

from finops.models import PriceVersion, UsageRow
from finops.pricing import PriceBook, compute_cost, consolidate, normalize_model, price_usage


def row(model="gpt-4", day=date(2026, 10, 1), dept="eng", p=1000, c=500, n=10, **kw):
    return UsageRow(day, dept, model, p, c, n, **kw)


class NormalizeTests(unittest.TestCase):
    def test_variants_collapse(self):
        self.assertEqual(normalize_model("GPT-4-0613"), "gpt-4")
        self.assertEqual(normalize_model("gpt-4o-2024-08-06"), "gpt-4o")
        self.assertEqual(normalize_model("gpt-35-turbo-0125"), "gpt-3.5-turbo")
        self.assertEqual(normalize_model("gpt-3.5-turbo"), "gpt-3.5-turbo")
        self.assertEqual(normalize_model("gpt-4"), "gpt-4")
        self.assertEqual(normalize_model("  "), "unknown")
        self.assertEqual(normalize_model(None), "unknown")


class PriceBookTests(unittest.TestCase):
    def setUp(self):
        self.book = PriceBook([
            ("gpt-4", PriceVersion(date(2000, 1, 1), Decimal("0.03"), Decimal("0.06"), "gpt-3.5-turbo")),
            ("gpt-4", PriceVersion(date(2026, 10, 10), Decimal("0.02"), Decimal("0.04"), "gpt-3.5-turbo")),
        ])

    def test_effective_dating(self):
        self.assertEqual(self.book.lookup("gpt-4", date(2026, 10, 9)).prompt_per_1k, Decimal("0.03"))
        self.assertEqual(self.book.lookup("gpt-4", date(2026, 10, 10)).prompt_per_1k, Decimal("0.02"))

    def test_unknown_model_and_too_early(self):
        self.assertIsNone(self.book.lookup("claude-x", date(2026, 10, 10)))
        self.assertIsNone(self.book.lookup("gpt-4", date(1999, 1, 1)))

    def test_lookup_uses_normalised_name(self):
        self.assertIsNotNone(self.book.lookup("gpt-4-0613", date(2026, 10, 11)))

    def test_cost(self):
        v = self.book.lookup("gpt-4", date(2026, 10, 1))
        self.assertEqual(compute_cost(1000, 500, v), Decimal("0.060000"))

    def test_price_usage_flags_unpriced(self):
        priced = price_usage([row(), row(model="mystery")], self.book)
        self.assertTrue(priced[0].priced)
        self.assertEqual(priced[0].cost, Decimal("0.060000") * 10 / 10)  # 1000p + 500c at old price
        self.assertFalse(priced[1].priced)
        self.assertEqual(priced[1].cost, Decimal("0"))


class ConsolidateTests(unittest.TestCase):
    def test_merges_aliases_sharing_a_key(self):
        merged = consolidate([
            row(model="gpt-4-0613", p=100, c=50, n=10, malformed_count=1, p95_total_tokens=300, avg_latency_ms=100, p95_latency_ms=200),
            row(model="gpt-4", p=300, c=150, n=30, malformed_count=2, p95_total_tokens=500, avg_latency_ms=200, p95_latency_ms=400),
        ])
        self.assertEqual(len(merged), 1)
        m = merged[0]
        self.assertEqual((m.model_name, m.prompt_tokens, m.completion_tokens, m.request_count, m.malformed_count),
                         ("gpt-4", 400, 200, 40, 3))
        self.assertEqual((m.p95_total_tokens, m.p95_latency_ms), (500, 400))
        self.assertEqual(m.avg_latency_ms, 175)  # request-weighted: (100*10 + 200*30) / 40

    def test_keeps_distinct_keys_sorted(self):
        out = consolidate([row(dept="b"), row(dept="a")])
        self.assertEqual([r.department_id for r in out], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
