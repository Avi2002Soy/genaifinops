import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from finops.ingest import (
    EventRejected, ingest_events, parse_event, percentile, price_aggregates, process_events, unpack_bodies, unpack_payload,
)
from finops.models import PriceVersion
from finops.pricing import PriceBook

NOW = datetime(2026, 10, 31, 10, 30, tzinfo=timezone.utc)


def ev(i=1, **kw):
    base = {"RequestId": f"req-{i}", "Timestamp": "2026-10-31T10:05:00Z", "DepartmentId": "eng",
            "ModelName": "gpt-4-0613", "PromptTokens": 1000, "CompletionTokens": 500, "LatencyMs": 1200}
    base.update(kw)
    return base


class ParseTests(unittest.TestCase):
    def test_valid_event_is_normalised(self):
        e = parse_event(ev(), now=NOW)
        self.assertEqual((e.department_id, e.model_name, e.prompt_tokens, e.latency_ms), ("eng", "gpt-4", 1000, 1200))
        self.assertEqual(e.occurred_at, datetime(2026, 10, 31, 10, 5, tzinfo=timezone.utc))

    def test_snake_case_keys_and_missing_dept(self):
        e = parse_event({"request_id": "x", "prompt_tokens": "5", "completion_tokens": 2, "model": "GPT-4o"}, now=NOW)
        self.assertEqual((e.department_id, e.model_name, e.occurred_at), ("Unknown", "gpt-4o", NOW))

    def test_rejections(self):
        bad = {
            "missing id": ev(RequestId=""), "usage missing (-1)": ev(PromptTokens=-1),
            "not a number": ev(CompletionTokens="abc"), "fraction": ev(PromptTokens=1.5),
            "bool": ev(PromptTokens=True), "absurd": ev(PromptTokens=10**9),
            "old": ev(Timestamp="2026-10-01T00:00:00Z"), "future": ev(Timestamp="2026-10-31T12:00:00Z"),
            "bad time": ev(Timestamp="yesterday-ish"), "not object": "oops",
        }
        for label, raw in bad.items():
            with self.subTest(label), self.assertRaises(EventRejected):
                parse_event(raw, now=NOW)

    def test_unpack_shapes(self):
        self.assertEqual(unpack_payload([1, 2]), [1, 2])
        self.assertEqual(unpack_payload({"events": [1]}), [1])
        self.assertEqual(unpack_payload({"a": 1}), [{"a": 1}])
        items = unpack_bodies([b'{"RequestId": "a"}', '[{"RequestId": "b"}, {"RequestId": "c"}]', "not json"])
        self.assertEqual(len(items), 4)
        self.assertIn("__unparseable__", items[-1])


class ProcessTests(unittest.TestCase):
    def test_dedupe_reject_and_aggregate(self):
        items = [ev(1), ev(2, PromptTokens=3000), ev(1), ev(3, PromptTokens=-1), ev(4, ModelName="gpt-4o")]
        r = process_events(items, now=NOW)
        self.assertEqual((r.received, r.accepted, r.duplicates, len(r.rejected)), (5, 3, 1, 1))
        self.assertAlmostEqual(r.capture_rate, 3 / 4)  # duplicates don't count as failures
        gpt4 = next(a for a in r.aggregates if a.model_name == "gpt-4")
        self.assertEqual((gpt4.request_count, gpt4.prompt_tokens, gpt4.completion_tokens), (2, 4000, 1000))
        self.assertEqual(gpt4.bucket, datetime(2026, 10, 31, 10, tzinfo=timezone.utc))
        self.assertEqual(gpt4.p95_total_tokens, 3500)

    def test_batch_id_is_order_independent_and_stable(self):
        a = process_events([ev(1), ev(2)], now=NOW).batch_id
        b = process_events([ev(2), ev(1), ev(1)], now=NOW).batch_id
        c = process_events([ev(1), ev(3)], now=NOW).batch_id
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(process_events([ev(1, PromptTokens=-1)], now=NOW).batch_id, "")

    def test_hour_buckets_split(self):
        r = process_events([ev(1), ev(2, Timestamp="2026-10-31T09:59:00Z")], now=NOW)
        self.assertEqual(len(r.aggregates), 2)

    def test_percentile_nearest_rank(self):
        self.assertEqual(percentile(list(range(1, 101)), 95), 95)
        self.assertEqual(percentile([7], 95), 7)
        self.assertIsNone(percentile([], 95))

    def test_pricing_marks_unpriced(self):
        book = PriceBook([("gpt-4", PriceVersion(date(2000, 1, 1), Decimal("0.03"), Decimal("0.06")))])
        r = process_events([ev(1), ev(2, ModelName="mystery")], now=NOW)
        priced = {a.model_name: (c, ok) for a, c, ok in price_aggregates(r.aggregates, book)}
        self.assertEqual(priced["gpt-4"], (Decimal("0.060000"), True))
        self.assertEqual(priced["mystery"], (Decimal("0"), False))


class FakeRepo:
    def __init__(self): self.claimed, self.hourly, self.dead = set(), [], []
    def claim_batch(self, batch_id, n):
        if batch_id in self.claimed: return False
        self.claimed.add(batch_id); return True
    def load_price_book(self):
        return PriceBook([("gpt-4", PriceVersion(date(2000, 1, 1), Decimal("0.03"), Decimal("0.06")))])
    def add_hourly_usage(self, priced): self.hourly += priced
    def save_dead_letters(self, batch_id, rejected): self.dead += rejected


class FakeDb:
    def __init__(self, repo): self.repo = repo
    def run(self, work): return work(self.repo)


class IngestFlowTests(unittest.TestCase):
    def test_replayed_batch_is_a_noop(self):
        repo, db = FakeRepo(), FakeDb(None)
        db.repo = repo
        items = [ev(1), ev(2), ev(9, PromptTokens=-1)]
        first = ingest_events(db, items, now=NOW)
        second = ingest_events(db, items, now=NOW)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(len(repo.hourly), 1)   # written once
        self.assertEqual(len(repo.dead), 1)     # dead-lettered once

    def test_all_bad_batch_still_dead_letters(self):
        repo = FakeRepo(); db = FakeDb(repo)
        r = ingest_events(db, [ev(1, PromptTokens=-1)], now=NOW)
        self.assertEqual((r.accepted, len(repo.dead), repo.hourly), (0, 1, []))

    def test_empty_batch_touches_nothing(self):
        class Boom:
            def run(self, work): raise AssertionError("DB must not be woken for an empty batch")
        ingest_events(Boom(), [], now=NOW)


if __name__ == "__main__":
    unittest.main()
