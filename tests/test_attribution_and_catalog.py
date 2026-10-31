import json
import unittest
from decimal import Decimal
from pathlib import Path

from finops.attribution import build_showback
from finops.catalog import CatalogError, parse_catalog
from finops.models import CostLine
from finops.pricing import PriceBook

CATALOG_FILE = Path(__file__).resolve().parents[1] / "database" / "pricing_catalog.json"


class ShowbackTests(unittest.TestCase):
    def test_shares_and_unit_costs(self):
        lines = [
            CostLine("support", "gpt-4", Decimal("60"), 1000, 600_000),
            CostLine("support", "gpt-4o-mini", Decimal("10"), 4000, 800_000),
            CostLine("sales", "gpt-4o-mini", Decimal("30"), 3000, 1_000_000),
        ]
        sb = build_showback(lines)
        self.assertEqual(sb["total_cost"], Decimal("100.00"))
        support, sales = sb["departments"]
        self.assertEqual((support["department_id"], support["share_pct"], support["top_model"]), ("support", 70.0, "gpt-4"))
        self.assertEqual(support["top_model_share_pct"], 85.7)
        self.assertEqual(sales["cost_per_request"], Decimal("0.010000"))
        mini = next(m for m in sb["models"] if m["model_name"] == "gpt-4o-mini")
        self.assertEqual((mini["cost"], mini["share_pct"]), (Decimal("40.00"), 40.0))
        self.assertEqual(mini["cost_per_1k_tokens"], Decimal("0.022222"))

    def test_empty(self):
        sb = build_showback([])
        self.assertEqual((sb["total_cost"], sb["departments"], sb["models"]), (Decimal("0.00"), [], []))


class CatalogTests(unittest.TestCase):
    def test_shipped_catalog_is_valid_and_prices_every_model(self):
        entries = parse_catalog(json.loads(CATALOG_FILE.read_text(encoding="utf-8")))
        self.assertGreaterEqual(len({e.provider for e in entries}), 4)
        book = PriceBook(e.to_price_entry() for e in entries)
        for e in entries:
            self.assertIsNotNone(book.lookup(e.name, __import__("datetime").date(2026, 10, 31)), e.name)

    def test_version_suffixed_deployment_names_resolve(self):
        entries = parse_catalog(json.loads(CATALOG_FILE.read_text(encoding="utf-8")))
        book = PriceBook(e.to_price_entry() for e in entries)
        today = __import__("datetime").date(2026, 10, 31)
        self.assertIsNotNone(book.lookup("claude-3-5-sonnet-20241022", today))
        self.assertIsNotNone(book.lookup("gpt-4o-2024-08-06", today))

    def test_validation_errors(self):
        def cat(*models): return {"models": list(models)}
        cheap = {"name": "a-mini", "provider": "p", "prompt_per_1k": 0.1, "completion_per_1k": 0.1}
        cases = {
            "unknown fallback": cat({"name": "a", "provider": "p", "prompt_per_1k": 1, "completion_per_1k": 1, "fallback": "ghost"}),
            "self fallback": cat({"name": "a", "provider": "p", "prompt_per_1k": 1, "completion_per_1k": 1, "fallback": "a"}),
            "pricier fallback": cat({"name": "a", "provider": "p", "prompt_per_1k": 0.01, "completion_per_1k": 0.01, "fallback": "a-mini"}, cheap),
            "cross provider": cat({"name": "a", "provider": "q", "prompt_per_1k": 1, "completion_per_1k": 1, "fallback": "a-mini"}, cheap),
            "negative": cat({"name": "a", "provider": "p", "prompt_per_1k": -1, "completion_per_1k": 1}),
            "not a number": cat({"name": "a", "provider": "p", "prompt_per_1k": "x", "completion_per_1k": 1}),
            "duplicate": cat(cheap, cheap),
            "bad date": cat({**cheap, "effective_from": "01/02/2026"}),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(CatalogError):
                parse_catalog(data)


if __name__ == "__main__":
    unittest.main()
