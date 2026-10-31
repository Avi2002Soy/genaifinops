import unittest
from datetime import date, datetime, timezone
from unittest import mock

from finops import db as dbmod
from finops.config import Settings
from finops.kql import rows_from_table


class FakeConn:
    def __init__(self): self.commits = self.rollbacks = self.closed = 0
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed += 1


def settings(attempts=3):
    return Settings("w", "c", sql_max_attempts=attempts, sql_retry_base_seconds=1.0)


class ClassifierTests(unittest.TestCase):
    def test_transient_detection(self):
        self.assertTrue(dbmod.is_transient(Exception("('HY000', 'Database ... is not currently available. (40613)')")))
        self.assertTrue(dbmod.is_transient(Exception("Login timeout expired (HYT00)")))
        self.assertFalse(dbmod.is_transient(Exception("Invalid object name 'dbo.Nope'. (208)")))

    def test_duplicate_key_detection(self):
        self.assertTrue(dbmod.is_duplicate_key(Exception("Violation of PRIMARY KEY constraint (2627)")))
        self.assertFalse(dbmod.is_duplicate_key(Exception("Timeout (HYT00)")))


class RetryTests(unittest.TestCase):
    def test_retries_transient_then_succeeds(self):
        conns, sleeps = [], []
        def connect(_):
            c = FakeConn(); conns.append(c); return c
        calls = {"n": 0}
        def work(_repo):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("Database is paused (40613)")
            return "ok"
        with mock.patch.object(dbmod, "_connect", connect):
            result = dbmod.Database(settings(), sleep=sleeps.append).run(work)
        self.assertEqual(result, "ok")
        self.assertEqual(sleeps, [1.0, 2.0])  # exponential backoff
        self.assertTrue(all(c.closed == 1 for c in conns))
        self.assertEqual(conns[-1].commits, 1)

    def test_non_transient_error_is_not_retried(self):
        with mock.patch.object(dbmod, "_connect", lambda _: FakeConn()):
            with self.assertRaises(ValueError):
                dbmod.Database(settings(), sleep=lambda s: self.fail("should not sleep")).run(
                    lambda _r: (_ for _ in ()).throw(ValueError("bad sql")))

    def test_gives_up_after_max_attempts(self):
        n = {"c": 0}
        def work(_r):
            n["c"] += 1; raise Exception("(40613)")
        with mock.patch.object(dbmod, "_connect", lambda _: FakeConn()):
            with self.assertRaises(Exception):
                dbmod.Database(settings(attempts=3), sleep=lambda s: None).run(work)
        self.assertEqual(n["c"], 3)


class KqlParsingTests(unittest.TestCase):
    def test_rows_from_table_handles_nulls_and_dates(self):
        cols = ["DepartmentId", "ModelName", "UsageDate", "TotalPromptTokens", "TotalCompletionTokens",
                "RequestCount", "MalformedCount", "P95TotalTokens", "AvgLatencyMs", "P95LatencyMs"]
        rows = [["eng", "gpt-4", datetime(2026, 10, 20, tzinfo=timezone.utc), 1000, 500, 10, 1, 812.4, None, float("nan")]]
        r = rows_from_table(cols, rows)[0]
        self.assertEqual(r.usage_date, date(2026, 10, 20))
        self.assertEqual((r.prompt_tokens, r.request_count, r.malformed_count, r.p95_total_tokens), (1000, 10, 1, 812))
        self.assertIsNone(r.avg_latency_ms)
        self.assertIsNone(r.p95_latency_ms)

    def test_iso_string_dates(self):
        cols = ["DepartmentId", "ModelName", "UsageDate"]
        r = rows_from_table(cols, [["eng", "gpt-4", "2026-10-20T00:00:00Z"]])[0]
        self.assertEqual(r.usage_date, date(2026, 10, 20))


if __name__ == "__main__":
    unittest.main()
