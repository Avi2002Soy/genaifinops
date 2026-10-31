"""Azure SQL access: connection (password or managed identity), retry, repository."""
from __future__ import annotations

import json
import logging
import struct
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from .config import Settings
from .models import (
    AcceptedRecommendation, Alert, CostLine, ImpactResult, Inefficiency, ModelStats, PriceVersion, PricedUsage,
    ReconciliationRow, RoutingRecommendation, RunSummary, WindowTotals,
)
from .pricing import PriceBook, normalize_model

T = TypeVar("T")

# Errors raised while a serverless DB is paused/resuming, during failover, or throttled.
_TRANSIENT_MARKERS = (
    "40613", "40197", "40501", "40540", "40544", "40549", "40550",
    "49918", "49919", "49920", "10928", "10929", "HYT00", "08S01", "08001",
)
_SQL_COPT_SS_ACCESS_TOKEN = 1256  # pyodbc attribute for AAD access tokens


def is_transient(exc: BaseException) -> bool:
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def is_duplicate_key(exc: BaseException) -> bool:
    text = str(exc)
    return "2627" in text or "2601" in text


_credential = None


def _connect(settings: Settings):
    import pyodbc  # lazy: only needed when talking to SQL

    if settings.sql_use_managed_identity:
        global _credential
        from azure.identity import DefaultAzureCredential

        _credential = _credential or DefaultAzureCredential()
        token = _credential.get_token("https://database.windows.net/.default").token.encode("utf-16-le")
        packed = struct.pack(f"<I{len(token)}s", len(token), token)
        return pyodbc.connect(
            settings.sql_connection_string, attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: packed}, timeout=60
        )
    return pyodbc.connect(settings.sql_connection_string, timeout=60)


class Database:
    """Opens a short-lived connection per unit of work and retries transient failures.

    Short-lived on purpose: an idle open session blocks serverless auto-pause and
    burns the free monthly vCore-seconds.
    """

    def __init__(self, settings: Settings, sleep: Callable[[float], None] = time.sleep):
        self._settings = settings
        self._sleep = sleep

    def run(self, work: Callable[["Repository"], T]) -> T:
        attempts = max(1, self._settings.sql_max_attempts)
        for attempt in range(1, attempts + 1):
            conn = None
            try:
                conn = _connect(self._settings)
                result = work(Repository(conn))
                conn.commit()
                return result
            except Exception as exc:  # noqa: BLE001
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                if attempt == attempts or not is_transient(exc):
                    raise
                delay = self._settings.sql_retry_base_seconds * 2 ** (attempt - 1)
                logging.warning("Transient SQL error (attempt %d/%d), retrying in %.0fs: %s", attempt, attempts, delay, exc)
                self._sleep(delay)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
        raise RuntimeError("unreachable")


_UPSERT_USAGE = """
MERGE dbo.DailyTokenUsage WITH (HOLDLOCK) AS t
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)) AS s
  (UsageDate, DepartmentId, ModelName, TotalPromptTokens, TotalCompletionTokens, RequestCount,
   MalformedCount, TotalCost, UnpricedFlag, P95TotalTokens, AvgLatencyMs, P95LatencyMs)
ON t.UsageDate = s.UsageDate AND t.DepartmentId = s.DepartmentId AND t.ModelName = s.ModelName
WHEN MATCHED THEN UPDATE SET
  TotalPromptTokens = s.TotalPromptTokens, TotalCompletionTokens = s.TotalCompletionTokens,
  RequestCount = s.RequestCount, MalformedCount = s.MalformedCount, TotalCost = s.TotalCost,
  UnpricedFlag = s.UnpricedFlag, P95TotalTokens = s.P95TotalTokens, AvgLatencyMs = s.AvgLatencyMs,
  P95LatencyMs = s.P95LatencyMs, UpdatedAtUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
  (UsageDate, DepartmentId, ModelName, TotalPromptTokens, TotalCompletionTokens, RequestCount,
   MalformedCount, TotalCost, UnpricedFlag, P95TotalTokens, AvgLatencyMs, P95LatencyMs)
  VALUES (s.UsageDate, s.DepartmentId, s.ModelName, s.TotalPromptTokens, s.TotalCompletionTokens,
          s.RequestCount, s.MalformedCount, s.TotalCost, s.UnpricedFlag, s.P95TotalTokens,
          s.AvgLatencyMs, s.P95LatencyMs);
"""

_UPSERT_RECOMMENDATION = """
MERGE dbo.RoutingRecommendations WITH (HOLDLOCK) AS t
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)) AS s
  (DepartmentId, CurrentModel, SuggestedModel, Requests, AvgPromptTokens, AvgCompletionTokens,
   P95TotalTokens, WindowCost, EstMonthlySavings, SavingsPct, Confidence, LatencyDeltaPct)
ON t.DepartmentId = s.DepartmentId AND t.CurrentModel = s.CurrentModel AND t.SuggestedModel = s.SuggestedModel
WHEN MATCHED THEN UPDATE SET
  Requests = s.Requests, AvgPromptTokens = s.AvgPromptTokens, AvgCompletionTokens = s.AvgCompletionTokens,
  P95TotalTokens = s.P95TotalTokens, WindowCost = s.WindowCost, EstMonthlySavings = s.EstMonthlySavings,
  SavingsPct = s.SavingsPct, Confidence = s.Confidence, LatencyDeltaPct = s.LatencyDeltaPct,
  Status = CASE WHEN t.Status = 'stale' THEN 'open' ELSE t.Status END, UpdatedAtUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
  (DepartmentId, CurrentModel, SuggestedModel, Requests, AvgPromptTokens, AvgCompletionTokens,
   P95TotalTokens, WindowCost, EstMonthlySavings, SavingsPct, Confidence, LatencyDeltaPct)
  VALUES (s.DepartmentId, s.CurrentModel, s.SuggestedModel, s.Requests, s.AvgPromptTokens,
          s.AvgCompletionTokens, s.P95TotalTokens, s.WindowCost, s.EstMonthlySavings, s.SavingsPct,
          s.Confidence, s.LatencyDeltaPct);
"""

# Additive on purpose: the hot path receives many small batches per hour. Replays are prevented by
# dbo.IngestBatches (claim_batch), not by overwriting.
_ADD_HOURLY = """
MERGE dbo.HourlyTokenUsage WITH (HOLDLOCK) AS t
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)) AS s
  (BucketHourUtc, DepartmentId, ModelName, TotalPromptTokens, TotalCompletionTokens, RequestCount,
   TotalCost, UnpricedFlag, P95TotalTokens, AvgLatencyMs, P95LatencyMs)
ON t.BucketHourUtc = s.BucketHourUtc AND t.DepartmentId = s.DepartmentId AND t.ModelName = s.ModelName
WHEN MATCHED THEN UPDATE SET
  TotalPromptTokens = t.TotalPromptTokens + s.TotalPromptTokens,
  TotalCompletionTokens = t.TotalCompletionTokens + s.TotalCompletionTokens,
  TotalCost = t.TotalCost + s.TotalCost,
  UnpricedFlag = CASE WHEN t.UnpricedFlag = 1 OR s.UnpricedFlag = 1 THEN 1 ELSE 0 END,
  P95TotalTokens = CASE WHEN s.P95TotalTokens IS NULL THEN t.P95TotalTokens
                        WHEN t.P95TotalTokens IS NULL OR s.P95TotalTokens > t.P95TotalTokens THEN s.P95TotalTokens
                        ELSE t.P95TotalTokens END,
  P95LatencyMs = CASE WHEN s.P95LatencyMs IS NULL THEN t.P95LatencyMs
                      WHEN t.P95LatencyMs IS NULL OR s.P95LatencyMs > t.P95LatencyMs THEN s.P95LatencyMs
                      ELSE t.P95LatencyMs END,
  AvgLatencyMs = CASE WHEN s.AvgLatencyMs IS NULL THEN t.AvgLatencyMs
                      WHEN t.AvgLatencyMs IS NULL THEN s.AvgLatencyMs
                      ELSE (CAST(t.AvgLatencyMs AS BIGINT) * t.RequestCount + CAST(s.AvgLatencyMs AS BIGINT) * s.RequestCount)
                           / NULLIF(t.RequestCount + s.RequestCount, 0) END,
  RequestCount = t.RequestCount + s.RequestCount,
  UpdatedAtUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
  (BucketHourUtc, DepartmentId, ModelName, TotalPromptTokens, TotalCompletionTokens, RequestCount,
   TotalCost, UnpricedFlag, P95TotalTokens, AvgLatencyMs, P95LatencyMs)
  VALUES (s.BucketHourUtc, s.DepartmentId, s.ModelName, s.TotalPromptTokens, s.TotalCompletionTokens,
          s.RequestCount, s.TotalCost, s.UnpricedFlag, s.P95TotalTokens, s.AvgLatencyMs, s.P95LatencyMs);
"""

_UPSERT_RECONCILIATION = """
MERGE dbo.CaptureReconciliation WITH (HOLDLOCK) AS t
USING (VALUES (?, ?, ?, ?, ?)) AS s (UsageDate, GatewayRequests, CapturedRequests, CaptureRate, Status)
ON t.UsageDate = s.UsageDate
WHEN MATCHED THEN UPDATE SET GatewayRequests = s.GatewayRequests, CapturedRequests = s.CapturedRequests,
  CaptureRate = s.CaptureRate, Status = s.Status, CheckedAtUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (UsageDate, GatewayRequests, CapturedRequests, CaptureRate, Status)
  VALUES (s.UsageDate, s.GatewayRequests, s.CapturedRequests, s.CaptureRate, s.Status);
"""

_UPSERT_INEFFICIENCY = """
MERGE dbo.CostInefficiencies WITH (HOLDLOCK) AS t
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?)) AS s
  (DepartmentId, ModelName, Kind, Severity, Requests, EstMonthlyWaste, Detail, Suggestion)
ON t.DepartmentId = s.DepartmentId AND t.ModelName = s.ModelName AND t.Kind = s.Kind
WHEN MATCHED THEN UPDATE SET Severity = s.Severity, Requests = s.Requests, EstMonthlyWaste = s.EstMonthlyWaste,
  Detail = s.Detail, Suggestion = s.Suggestion,
  Status = CASE WHEN t.Status = 'stale' THEN 'open' ELSE t.Status END, UpdatedAtUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (DepartmentId, ModelName, Kind, Severity, Requests, EstMonthlyWaste, Detail, Suggestion)
  VALUES (s.DepartmentId, s.ModelName, s.Kind, s.Severity, s.Requests, s.EstMonthlyWaste, s.Detail, s.Suggestion);
"""

_UPSERT_IMPACT = """
MERGE dbo.RoutingImpact WITH (HOLDLOCK) AS t
USING (VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)) AS s
  (DepartmentId, CurrentModel, SuggestedModel, AcceptedOn, Status, BeforeRequests, AfterRequests,
   BeforeCostPerRequest, AfterCostPerRequest, AvoidedCost, CostReductionPct, BeforeLatencyMs, AfterLatencyMs, LatencyChangePct)
ON t.DepartmentId = s.DepartmentId AND t.CurrentModel = s.CurrentModel AND t.SuggestedModel = s.SuggestedModel
WHEN MATCHED THEN UPDATE SET AcceptedOn = s.AcceptedOn, Status = s.Status, BeforeRequests = s.BeforeRequests,
  AfterRequests = s.AfterRequests, BeforeCostPerRequest = s.BeforeCostPerRequest, AfterCostPerRequest = s.AfterCostPerRequest,
  AvoidedCost = s.AvoidedCost, CostReductionPct = s.CostReductionPct, BeforeLatencyMs = s.BeforeLatencyMs,
  AfterLatencyMs = s.AfterLatencyMs, LatencyChangePct = s.LatencyChangePct, ComputedAtUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (DepartmentId, CurrentModel, SuggestedModel, AcceptedOn, Status, BeforeRequests, AfterRequests,
   BeforeCostPerRequest, AfterCostPerRequest, AvoidedCost, CostReductionPct, BeforeLatencyMs, AfterLatencyMs, LatencyChangePct)
  VALUES (s.DepartmentId, s.CurrentModel, s.SuggestedModel, s.AcceptedOn, s.Status, s.BeforeRequests, s.AfterRequests,
   s.BeforeCostPerRequest, s.AfterCostPerRequest, s.AvoidedCost, s.CostReductionPct, s.BeforeLatencyMs, s.AfterLatencyMs, s.LatencyChangePct);
"""

_UPSERT_PRICE = """
MERGE dbo.ModelPricing WITH (HOLDLOCK) AS t
USING (VALUES (?, ?, ?, ?, ?, ?)) AS s (ModelName, EffectiveFrom, PromptCostPer1K, CompletionCostPer1K, FallbackModel, Provider)
ON t.ModelName = s.ModelName AND t.EffectiveFrom = s.EffectiveFrom
WHEN MATCHED THEN UPDATE SET PromptCostPer1K = s.PromptCostPer1K, CompletionCostPer1K = s.CompletionCostPer1K,
  FallbackModel = s.FallbackModel, Provider = s.Provider
WHEN NOT MATCHED THEN INSERT (ModelName, EffectiveFrom, PromptCostPer1K, CompletionCostPer1K, FallbackModel, Provider)
  VALUES (s.ModelName, s.EffectiveFrom, s.PromptCostPer1K, s.CompletionCostPer1K, s.FallbackModel, s.Provider);
"""


class Repository:
    """All SQL lives here so the rest of the code stays testable with a fake."""

    def __init__(self, conn: Any):
        self._conn = conn

    def commit(self) -> None:
        self._conn.commit()

    # -- reference data -----------------------------------------------------
    def load_price_book(self) -> PriceBook:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT ModelName, EffectiveFrom, PromptCostPer1K, CompletionCostPer1K, FallbackModel "
            "FROM dbo.ModelPricing"
        )
        entries = [
            (r[0], PriceVersion(r[1], Decimal(r[2]), Decimal(r[3]), normalize_model(r[4]) if r[4] else None))
            for r in cur.fetchall()
        ]
        return PriceBook(entries)

    def budgets(self) -> Dict[str, Decimal]:
        cur = self._conn.cursor()
        cur.execute("SELECT DepartmentId, MonthlyBudgetUsd FROM dbo.Budgets")
        return {r[0]: Decimal(r[1]) for r in cur.fetchall()}

    def upsert_budget(self, department_id: str, amount: Decimal) -> None:
        self._conn.cursor().execute(
            "MERGE dbo.Budgets AS t USING (VALUES (?, ?)) AS s (DepartmentId, Amt) ON t.DepartmentId = s.DepartmentId "
            "WHEN MATCHED THEN UPDATE SET MonthlyBudgetUsd = s.Amt "
            "WHEN NOT MATCHED THEN INSERT (DepartmentId, MonthlyBudgetUsd) VALUES (s.DepartmentId, s.Amt);",
            department_id, amount,
        )

    # -- usage --------------------------------------------------------------
    def upsert_usage(self, rows: Sequence[PricedUsage]) -> int:
        cur = self._conn.cursor()
        for p in rows:
            u = p.usage
            cur.execute(
                _UPSERT_USAGE, u.usage_date, u.department_id, u.model_name, u.prompt_tokens,
                u.completion_tokens, u.request_count, u.malformed_count, p.cost, 0 if p.priced else 1,
                u.p95_total_tokens, u.avg_latency_ms, u.p95_latency_ms,
            )
        return len(rows)

    def daily_costs(self, since: date) -> Dict[str, Dict[date, Decimal]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT DepartmentId, UsageDate, SUM(TotalCost) FROM dbo.DailyTokenUsage "
            "WHERE UsageDate >= ? GROUP BY DepartmentId, UsageDate",
            since,
        )
        out: Dict[str, Dict[date, Decimal]] = defaultdict(dict)
        for dept, day, cost in cur.fetchall():
            out[dept][day] = Decimal(cost)
        return dict(out)

    def model_stats(self, since: date) -> List[ModelStats]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT DepartmentId, ModelName, SUM(RequestCount), SUM(TotalPromptTokens), SUM(TotalCompletionTokens), "
            "MAX(P95TotalTokens), "
            "SUM(CAST(AvgLatencyMs AS FLOAT) * RequestCount) / NULLIF(SUM(CASE WHEN AvgLatencyMs IS NOT NULL THEN RequestCount END), 0), "
            "SUM(MalformedCount) "
            "FROM dbo.DailyTokenUsage WHERE UsageDate >= ? GROUP BY DepartmentId, ModelName",
            since,
        )
        return [
            ModelStats(r[0], r[1], int(r[2] or 0), int(r[3] or 0), int(r[4] or 0),
                       None if r[5] is None else int(r[5]), None if r[6] is None else float(r[6]),
                       int(r[7] or 0))
            for r in cur.fetchall()
        ]

    # -- alerts -------------------------------------------------------------
    def insert_new_alerts(self, alerts: Sequence[Alert]) -> List[Alert]:
        """Insert alerts, returning only those never seen before (dedupe by AlertKey)."""
        created: List[Alert] = []
        cur = self._conn.cursor()
        for a in alerts:
            try:
                cur.execute(
                    "INSERT INTO dbo.Alerts (AlertKey, AlertType, Severity, DepartmentId, Message, Payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    a.key, a.type, a.severity, a.department_id, a.message[:1000], json.dumps(a.payload, default=str),
                )
                created.append(a)
            except Exception as exc:  # noqa: BLE001
                if not is_duplicate_key(exc):
                    raise
        return created

    def mark_notified(self, keys: Sequence[str]) -> None:
        cur = self._conn.cursor()
        for k in keys:
            cur.execute("UPDATE dbo.Alerts SET NotifiedAtUtc = SYSUTCDATETIME() WHERE AlertKey = ?", k)

    # -- routing ------------------------------------------------------------
    def save_recommendations(self, recs: Sequence[RoutingRecommendation], run_started: datetime) -> None:
        cur = self._conn.cursor()
        for r in recs:
            cur.execute(
                _UPSERT_RECOMMENDATION, r.department_id, r.current_model, r.suggested_model, r.requests,
                r.avg_prompt_tokens, r.avg_completion_tokens, r.p95_total_tokens, r.window_cost,
                r.est_monthly_savings, Decimal(str(r.savings_pct)), r.confidence,
                None if r.latency_delta_pct is None else Decimal(str(r.latency_delta_pct)),
            )
        # Anything still 'open' that this review no longer supports becomes 'stale'.
        cur.execute(
            "UPDATE dbo.RoutingRecommendations SET Status = 'stale' WHERE Status = 'open' AND UpdatedAtUtc < ?",
            run_started,
        )

    def purge_prefix(self, prefix: str) -> None:
        """Delete demo data whose DepartmentId starts with `prefix` (used by scripts/seed_sample_data.py)."""
        like = prefix.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]") + "%"
        cur = self._conn.cursor()
        for table in ("DailyTokenUsage", "HourlyTokenUsage", "Budgets", "Alerts", "RoutingRecommendations", "CostInefficiencies", "RoutingImpact"):
            cur.execute(f"DELETE FROM dbo.{table} WHERE DepartmentId LIKE ?", like)

    # -- run log ------------------------------------------------------------
    def record_run(self, s: RunSummary, started: datetime) -> None:
        self._conn.cursor().execute(
            "INSERT INTO dbo.PipelineRuns (RunType, StartedAtUtc, Status, WindowStart, WindowEnd, RowsWritten, "
            "Requests, MalformedCount, UnpricedRows, AlertsCreated, DurationSeconds, Message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            s.run_type, started.replace(tzinfo=None), s.status, s.window_start, s.window_end, s.rows_written, s.requests,
            s.malformed, s.unpriced_rows, s.alerts_created, Decimal(str(round(s.duration_seconds, 2))),
            "; ".join(s.warnings)[:2000] or None,
        )

    def recent_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT TOP (?) RunType, StartedAtUtc, Status, RowsWritten, Requests, MalformedCount, AlertsCreated, Message "
            "FROM dbo.PipelineRuns ORDER BY RunId DESC",
            limit,
        )
        cols = ["run_type", "started_at_utc", "status", "rows_written", "requests", "malformed", "alerts_created", "message"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # -- real-time ingest (hot path) ----------------------------------------
    def claim_batch(self, batch_id: str, event_count: int) -> bool:
        """Record a batch id; False means this exact batch was already stored (a replay)."""
        try:
            self._conn.cursor().execute(
                "INSERT INTO dbo.IngestBatches (BatchId, EventCount) VALUES (?, ?)", batch_id, event_count
            )
        except Exception as exc:  # noqa: BLE001
            if is_duplicate_key(exc):
                return False
            raise
        return True

    def add_hourly_usage(self, priced: Sequence[Tuple[Any, Decimal, bool]]) -> int:
        cur = self._conn.cursor()
        for a, cost, was_priced in priced:
            cur.execute(
                _ADD_HOURLY, a.bucket.replace(tzinfo=None), a.department_id, a.model_name, a.prompt_tokens,
                a.completion_tokens, a.request_count, cost, 0 if was_priced else 1, a.p95_total_tokens,
                a.avg_latency_ms, a.p95_latency_ms,
            )
        return len(priced)

    def save_dead_letters(self, batch_id: str, rejected: Sequence[Any], limit: int = 200) -> None:
        cur = self._conn.cursor()
        for r in list(rejected)[:limit]:  # cap per batch: a flood of garbage must not flood the table
            cur.execute(
                "INSERT INTO dbo.DeadLetterEvents (BatchId, Reason, RawEvent) VALUES (?, ?, ?)",
                batch_id[:64], r.reason[:200], r.raw[:500],
            )

    def live_totals(self, day: date) -> List[Dict[str, Any]]:
        """Near-real-time spend for one UTC day from the hourly table."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT DepartmentId, SUM(RequestCount), SUM(TotalCost) FROM dbo.HourlyTokenUsage "
            "WHERE BucketHourUtc >= ? AND BucketHourUtc < DATEADD(day, 1, ?) GROUP BY DepartmentId",
            datetime.combine(day, dtime.min), datetime.combine(day, dtime.min),
        )
        return [{"department_id": r[0], "requests": int(r[1]), "cost": Decimal(r[2])} for r in cur.fetchall()]

    def purge_old_ingest(self, cutoff: datetime) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM dbo.HourlyTokenUsage WHERE BucketHourUtc < ?", cutoff)
        cur.execute("DELETE FROM dbo.IngestBatches WHERE ReceivedAtUtc < ?", cutoff)
        cur.execute("DELETE FROM dbo.DeadLetterEvents WHERE ReceivedAtUtc < ?", cutoff)

    # -- capture reconciliation -------------------------------------------------
    def captured_counts(self, start: date, end: date) -> Dict[date, int]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT UsageDate, SUM(RequestCount) FROM dbo.DailyTokenUsage "
            "WHERE UsageDate >= ? AND UsageDate < ? GROUP BY UsageDate",
            start, end,
        )
        return {r[0]: int(r[1]) for r in cur.fetchall()}

    def upsert_reconciliation(self, rows: Sequence[ReconciliationRow]) -> None:
        cur = self._conn.cursor()
        for r in rows:
            cur.execute(
                _UPSERT_RECONCILIATION, r.usage_date, r.gateway_requests, r.captured_requests,
                None if r.capture_rate is None else Decimal(str(r.capture_rate)), r.status,
            )

    # -- inefficiencies & attribution -----------------------------------------
    def save_inefficiencies(self, items: Sequence[Inefficiency], run_started: datetime) -> None:
        cur = self._conn.cursor()
        for i in items:
            cur.execute(
                _UPSERT_INEFFICIENCY, i.department_id, i.model_name, i.kind, i.severity, i.requests,
                i.est_monthly_waste, i.detail[:500], i.suggestion[:300],
            )
        cur.execute(
            "UPDATE dbo.CostInefficiencies SET Status = 'stale' WHERE Status = 'open' AND UpdatedAtUtc < ?", run_started
        )

    def cost_lines(self, since: date) -> List[CostLine]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT DepartmentId, ModelName, SUM(TotalCost), SUM(RequestCount), "
            "SUM(TotalPromptTokens + TotalCompletionTokens) FROM dbo.DailyTokenUsage "
            "WHERE UsageDate >= ? GROUP BY DepartmentId, ModelName",
            since,
        )
        return [CostLine(r[0], r[1], Decimal(r[2]), int(r[3]), int(r[4])) for r in cur.fetchall()]

    def capture_summary(self, since: date) -> Dict[str, Any]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT SUM(GatewayRequests), SUM(CapturedRequests) FROM dbo.CaptureReconciliation "
            "WHERE UsageDate >= ? AND Status IN ('ok', 'gap')", since,
        )
        gw, cap = cur.fetchone()
        cur.execute(
            "SELECT SUM(RequestCount), SUM(MalformedCount) FROM dbo.DailyTokenUsage WHERE UsageDate >= ?", since
        )
        good, bad = cur.fetchone()
        good, bad, gw, cap = int(good or 0), int(bad or 0), int(gw or 0), int(cap or 0)
        return {
            "parse_rate": None if good + bad == 0 else round(good / (good + bad), 6),
            "gateway_capture_rate": None if gw == 0 else round(cap / gw, 6),
            "gateway_requests_checked": gw,
        }

    def open_opportunities(self) -> Dict[str, Decimal]:
        cur = self._conn.cursor()
        cur.execute("SELECT COALESCE(SUM(EstMonthlySavings), 0) FROM dbo.RoutingRecommendations WHERE Status = 'open'")
        routing = Decimal(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(SUM(EstMonthlyWaste), 0) FROM dbo.CostInefficiencies WHERE Status = 'open' AND Kind <> 'telemetry_gap'")
        waste = Decimal(cur.fetchone()[0])
        return {"routing_monthly_usd": routing, "inefficiency_monthly_usd": waste}

    # -- routing lifecycle & impact ---------------------------------------------
    def set_recommendation_status(
        self, department_id: str, current_model: str, suggested_model: str, status: str,
        accepted_on: Optional[date] = None,
    ) -> bool:
        stamp = datetime.combine(accepted_on, dtime.min) if accepted_on else None
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE dbo.RoutingRecommendations SET Status = ?, "
            "AcceptedAtUtc = CASE WHEN ? = 'accepted' THEN COALESCE(?, AcceptedAtUtc, SYSUTCDATETIME()) ELSE AcceptedAtUtc END, "
            "UpdatedAtUtc = SYSUTCDATETIME() "
            "WHERE DepartmentId = ? AND CurrentModel = ? AND SuggestedModel = ?",
            status, status, stamp, department_id, current_model, suggested_model,
        )
        return cur.rowcount > 0

    def accepted_recommendations(self) -> List[AcceptedRecommendation]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT DepartmentId, CurrentModel, SuggestedModel, CAST(AcceptedAtUtc AS DATE) "
            "FROM dbo.RoutingRecommendations WHERE Status = 'accepted' AND AcceptedAtUtc IS NOT NULL"
        )
        return [AcceptedRecommendation(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]

    def window_totals(self, department_id: str, start: date, end: date) -> WindowTotals:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(RequestCount), 0), COALESCE(SUM(TotalCost), 0), "
            "SUM(CAST(AvgLatencyMs AS FLOAT) * RequestCount) / NULLIF(SUM(CASE WHEN AvgLatencyMs IS NOT NULL THEN RequestCount END), 0) "
            "FROM dbo.DailyTokenUsage WHERE DepartmentId = ? AND UsageDate >= ? AND UsageDate < ?",
            department_id, start, end,
        )
        r = cur.fetchone()
        return WindowTotals(int(r[0]), Decimal(r[1]), None if r[2] is None else float(r[2]))

    def save_impact(self, results: Sequence[ImpactResult]) -> None:
        cur = self._conn.cursor()
        for r in results:
            cur.execute(
                _UPSERT_IMPACT, r.department_id, r.current_model, r.suggested_model, r.accepted_on, r.status,
                r.before_requests, r.after_requests, r.before_cost_per_request, r.after_cost_per_request,
                r.avoided_cost, None if r.cost_reduction_pct is None else Decimal(str(r.cost_reduction_pct)),
                None if r.before_latency_ms is None else Decimal(str(round(r.before_latency_ms, 1))),
                None if r.after_latency_ms is None else Decimal(str(round(r.after_latency_ms, 1))),
                None if r.latency_change_pct is None else Decimal(str(r.latency_change_pct)),
            )

    def impact_results(self) -> List[ImpactResult]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT DepartmentId, CurrentModel, SuggestedModel, AcceptedOn, Status, BeforeRequests, AfterRequests, "
            "BeforeCostPerRequest, AfterCostPerRequest, AvoidedCost, CostReductionPct, BeforeLatencyMs, AfterLatencyMs, "
            "LatencyChangePct FROM dbo.RoutingImpact"
        )
        def d(v): return None if v is None else Decimal(v)
        def f(v): return None if v is None else float(v)
        return [
            ImpactResult(r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6]), d(r[7]), d(r[8]), Decimal(r[9]),
                         f(r[10]), f(r[11]), f(r[12]), f(r[13]))
            for r in cur.fetchall()
        ]

    # -- pricing catalog ------------------------------------------------------------
    def upsert_prices(self, entries: Sequence[Any]) -> int:
        cur = self._conn.cursor()
        for e in entries:
            cur.execute(
                _UPSERT_PRICE, e.name, e.effective_from, e.prompt_per_1k, e.completion_per_1k, e.fallback, e.provider
            )
        return len(entries)
