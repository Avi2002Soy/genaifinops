import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List

from finops.config import Settings
from finops.models import Alert, PriceVersion, PricedUsage, UsageRow
from finops.pipeline import day_window, run_daily_sync
from finops.pricing import PriceBook

TODAY = date(2026, 10, 21)  # as_of (last complete day) = 2026-10-20


class FakeSource:
    def __init__(self, rows): self.rows, self.calls = rows, []
    def fetch(self, start, end):
        self.calls.append((start, end)); return self.rows


class FakeRepo:
    def __init__(self, history=None, budgets=None):
        self.history, self._budgets = history or {}, budgets or {}
        self.usage: List[PricedUsage] = []; self.alerts: Dict[str, Alert] = {}
        self.notified: List[str] = []; self.runs = []; self.commits = 0
    def load_price_book(self):
        return PriceBook([
            ("gpt-4", PriceVersion(date(2000, 1, 1), Decimal("0.03"), Decimal("0.06"), "gpt-3.5-turbo")),
        ])
    def upsert_usage(self, rows): self.usage = list(rows); return len(rows)
    def commit(self): self.commits += 1
    def daily_costs(self, since): return self.history
    def budgets(self): return self._budgets
    def insert_new_alerts(self, alerts):
        new = [a for a in alerts if a.key not in self.alerts]
        self.alerts.update({a.key: a for a in new}); return new
    def mark_notified(self, keys): self.notified += list(keys)
    def record_run(self, summary, started): self.runs.append(summary)


class FakeDb:
    def __init__(self, repo): self.repo = repo
    def run(self, work): return work(self.repo)


class FakeNotifier:
    def __init__(self): self.sent = []
    def send(self, alerts): self.sent.append(list(alerts)); return True


def settings(**kw):
    return Settings(workspace_id="w", sql_connection_string="c", **kw)


def usage(day, model="gpt-4", p=1000, c=500, n=10, bad=0):
    return UsageRow(day, "eng", model, p, c, n, malformed_count=bad, p95_total_tokens=800)


class WindowTests(unittest.TestCase):
    def test_window_is_complete_utc_days(self):
        start, end = day_window(TODAY, 2)
        self.assertEqual(start, datetime(2026, 10, 19, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 21, tzinfo=timezone.utc))


class PipelineTests(unittest.TestCase):
    def test_happy_path_writes_prices_and_records_run(self):
        repo, notifier = FakeRepo(), FakeNotifier()
        src = FakeSource([usage(date(2026, 10, 20))])
        s = run_daily_sync(settings(), src, FakeDb(repo), notifier, today=TODAY)
        self.assertEqual(s.status, "success")
        self.assertEqual((s.rows_written, s.requests, s.unpriced_rows), (1, 10, 0))
        self.assertEqual(repo.usage[0].cost, Decimal("0.060000"))
        self.assertEqual(len(repo.runs), 1)
        self.assertGreaterEqual(repo.commits, 1)
        self.assertEqual(src.calls[0][1], datetime(2026, 10, 21, tzinfo=timezone.utc))

    def test_unpriced_model_raises_one_alert_and_notifies_once(self):
        repo, notifier = FakeRepo(), FakeNotifier()
        src = FakeSource([usage(date(2026, 10, 20), model="mystery")])
        s1 = run_daily_sync(settings(), src, FakeDb(repo), notifier, today=TODAY)
        s2 = run_daily_sync(settings(), src, FakeDb(repo), notifier, today=TODAY)
        self.assertEqual(s1.alerts_created, 1)
        self.assertEqual(s2.alerts_created, 0)  # deduped by alert key
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(repo.notified, ["unpriced_model:mystery"])

    def test_capture_rate_alert(self):
        repo, notifier = FakeRepo(), FakeNotifier()
        src = FakeSource([usage(date(2026, 10, 20), n=980, bad=40)])
        s = run_daily_sync(settings(), src, FakeDb(repo), notifier, today=TODAY)
        self.assertAlmostEqual(s.capture_rate, 980 / 1020)
        self.assertIn("capture_rate:2026-10-20", repo.alerts)

    def test_budget_and_anomaly_alerts_use_history(self):
        hist = {"eng": {date(2026, 10, 20) - timedelta(days=i): Decimal("10") for i in range(1, 15)}}
        hist["eng"][date(2026, 10, 20)] = Decimal("90")
        repo = FakeRepo(history=hist, budgets={"eng": Decimal("100")})
        run_daily_sync(settings(), FakeSource([usage(date(2026, 10, 20))]), FakeDb(repo), FakeNotifier(), today=TODAY)
        types = {a.type for a in repo.alerts.values()}
        self.assertIn("anomaly", types)
        self.assertIn("budget_critical", types)

    def test_analytics_failure_does_not_lose_ingestion(self):
        repo = FakeRepo()
        repo.daily_costs = lambda since: (_ for _ in ()).throw(RuntimeError("boom"))
        s = run_daily_sync(settings(), FakeSource([usage(date(2026, 10, 20))]), FakeDb(repo), FakeNotifier(), today=TODAY)
        self.assertEqual(s.status, "partial")
        self.assertEqual(s.rows_written, 1)
        self.assertEqual(len(repo.usage), 1)

    def test_source_failure_records_failed_run_and_reraises(self):
        class Bad:
            def fetch(self, *_): raise RuntimeError("LA down")
        repo = FakeRepo()
        with self.assertRaises(RuntimeError):
            run_daily_sync(settings(), Bad(), FakeDb(repo), FakeNotifier(), today=TODAY)
        self.assertEqual(repo.runs[0].status, "failed")

    def test_backfill_mode_skips_analytics(self):
        repo, notifier = FakeRepo(), FakeNotifier()
        s = run_daily_sync(settings(), FakeSource([usage(date(2026, 10, 20), model="mystery")]), FakeDb(repo),
                           notifier, today=TODAY, run_type="backfill", analytics=False)
        self.assertEqual((s.alerts_created, repo.alerts, notifier.sent), (0, {}, []))


if __name__ == "__main__":
    unittest.main()
