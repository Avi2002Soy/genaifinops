import unittest
from datetime import date, datetime, timezone

from finops.config import Settings
from finops.reconcile import reconcile, reconciliation_alerts, run_capture_reconciliation

D1, D2, D3, D4 = date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31)


class ReconcileTests(unittest.TestCase):
    def test_statuses(self):
        rows = reconcile(
            captured={D1: 99_950, D2: 90_000, D3: 120_000, D4: 50},
            gateway={D1: 100_000, D2: 100_000, D3: 100_000, D4: 60},
            days=[D1, D2, D3, D4, date(2026, 10, 27)],
        )
        self.assertEqual([r.status for r in rows], ["ok", "gap", "over_capture", "ok", "no_gateway_data"])
        self.assertEqual(rows[0].capture_rate, 0.9995)
        self.assertIsNone(rows[4].capture_rate)   # no gateway data for that day
        self.assertEqual(rows[3].status, "ok")      # 83% but only 60 gateway requests: too small to alert on

    def test_alerts_only_for_gap_and_over_capture(self):
        rows = reconcile({D1: 99_000, D2: 120_000, D3: 100_000}, {D1: 100_000, D2: 100_000, D3: 100_000}, [D1, D2, D3])
        alerts = reconciliation_alerts(rows, 0.999)
        self.assertEqual({a.key for a in alerts}, {f"capture_gap:{D1}", f"capture_over:{D2}"})
        self.assertEqual(next(a for a in alerts if a.key.startswith("capture_gap")).severity, "critical")


class FakeMetrics:
    def __init__(self, counts): self.counts, self.calls = counts, []
    def request_counts(self, start, end): self.calls.append((start, end)); return self.counts


class FakeRepo:
    def __init__(self, captured): self.captured, self.rows, self.alerts, self.notified, self.runs = captured, [], {}, [], []
    def captured_counts(self, start, end): return self.captured
    def upsert_reconciliation(self, rows): self.rows = list(rows)
    def insert_new_alerts(self, alerts):
        new = [a for a in alerts if a.key not in self.alerts]; self.alerts.update({a.key: a for a in new}); return new
    def mark_notified(self, keys): self.notified += list(keys)
    def record_run(self, s, started): self.runs.append(s)


class FakeDb:
    def __init__(self, repo): self.repo = repo
    def run(self, work): return work(self.repo)


class FakeNotifier:
    def __init__(self): self.sent = []
    def send(self, alerts): self.sent.append(list(alerts)); return True


class RunTests(unittest.TestCase):
    def test_run_writes_alerts_once(self):
        s = Settings("7c3e9a12", "c", reconcile_lookback_days=2)
        today = date(2026, 10, 31)
        metrics = FakeMetrics({date(2026, 10, 29): 100_000, date(2026, 10, 30): 100_000})
        repo = FakeRepo({date(2026, 10, 29): 100_000, date(2026, 10, 30): 98_000})
        notifier = FakeNotifier()
        s1 = run_capture_reconciliation(s, metrics, FakeDb(repo), notifier, today=today)
        s2 = run_capture_reconciliation(s, metrics, FakeDb(repo), notifier, today=today)
        self.assertEqual((s1.alerts_created, s2.alerts_created, len(notifier.sent)), (1, 0, 1))
        self.assertEqual(metrics.calls[0][1], datetime(2026, 10, 31, tzinfo=timezone.utc))
        self.assertEqual(repo.rows[1].status, "gap")


if __name__ == "__main__":
    unittest.main()
