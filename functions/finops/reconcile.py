"""Capture-reliability reconciliation.

`vw_CaptureReliability` can only see events that *reached* Log Analytics. To claim a capture rate you
need an independent denominator: the gateway's own request counter. This module compares

    captured (usable events in DailyTokenUsage)   vs.   APIM `Requests` metric (Azure Monitor)

per complete UTC day and raises an alert when the ratio drops below the configured floor (99.9 % by default).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional, Protocol, Sequence

from .config import Settings
from .models import Alert, ReconciliationRow, RunSummary


class GatewayMetricsSource(Protocol):
    def request_counts(self, start: datetime, end: datetime) -> Dict[date, int]: ...


class ApimMetricsSource:
    """Reads the APIM `Requests` metric (daily TOTAL) through Azure Monitor.

    Needs the *Monitoring Reader* role on the APIM resource for the Function's managed identity.
    """

    def __init__(self, resource_id: str, metric_filter: str = "GatewayResponseCode eq '200'"):
        from azure.identity import DefaultAzureCredential
        from azure.monitor.query import MetricsQueryClient

        self._resource_id = resource_id
        self._filter = metric_filter
        self._client = MetricsQueryClient(DefaultAzureCredential())

    def request_counts(self, start: datetime, end: datetime) -> Dict[date, int]:
        from azure.monitor.query import MetricAggregationType

        response = self._client.query_resource(
            self._resource_id,
            metric_names=["Requests"],
            timespan=(start, end),
            granularity=timedelta(days=1),
            aggregations=[MetricAggregationType.TOTAL],
            filter=self._filter or None,
        )
        counts: Dict[date, int] = {}
        for metric in response.metrics:
            for series in metric.timeseries:
                for point in series.data:
                    if point.total is not None:
                        day = point.timestamp.astimezone(timezone.utc).date()
                        counts[day] = counts.get(day, 0) + int(point.total)
        logging.info("APIM metric returned %d day(s) for %s..%s", len(counts), start.date(), end.date())
        return counts


def reconcile(
    captured: Dict[date, int],
    gateway: Dict[date, int],
    days: Sequence[date],
    *,
    floor: float = 0.999,
    min_gateway_requests: int = 1000,
) -> List[ReconciliationRow]:
    rows: List[ReconciliationRow] = []
    for day in days:
        gw, cap = gateway.get(day, 0), captured.get(day, 0)
        if gw <= 0:
            rows.append(ReconciliationRow(day, gw, cap, None, "no_gateway_data"))
            continue
        rate = cap / gw
        if rate > 1.001:
            status = "over_capture"  # numerator > denominator: the metric filter doesn't match the policy
        elif rate < floor and gw >= min_gateway_requests:
            status = "gap"
        else:
            status = "ok"
        rows.append(ReconciliationRow(day, gw, cap, round(rate, 6), status))
    return rows


def reconciliation_alerts(rows: Sequence[ReconciliationRow], floor: float) -> List[Alert]:
    alerts: List[Alert] = []
    for r in rows:
        if r.status == "gap":
            lost = r.gateway_requests - r.captured_requests
            alerts.append(Alert(
                f"capture_gap:{r.usage_date}", "capture_gap", "critical",
                f"Only {r.capture_rate:.3%} of gateway requests on {r.usage_date} produced usable telemetry "
                f"({lost:,} missing of {r.gateway_requests:,}; floor {floor:.1%}).",
                None, {"gateway": r.gateway_requests, "captured": r.captured_requests},
            ))
        elif r.status == "over_capture":
            alerts.append(Alert(
                f"capture_over:{r.usage_date}", "capture_gap", "info",
                f"Captured events exceed gateway requests on {r.usage_date}: check APIM_METRIC_FILTER "
                f"(it should cover the same API/status codes the policy emits for).",
                None, {"gateway": r.gateway_requests, "captured": r.captured_requests},
            ))
    return alerts


def run_capture_reconciliation(
    settings: Settings, metrics: GatewayMetricsSource, db, notifier, *, today: Optional[date] = None
) -> RunSummary:
    t0 = time.monotonic()
    started = datetime.now(timezone.utc)
    today = today or started.date()
    first = today - timedelta(days=settings.reconcile_lookback_days)
    days = [first + timedelta(days=i) for i in range(settings.reconcile_lookback_days)]
    start = datetime.combine(first, dtime.min, tzinfo=timezone.utc)
    end = datetime.combine(today, dtime.min, tzinfo=timezone.utc)
    summary = RunSummary(run_type="reconcile", window_start=first, window_end=today)

    gateway = metrics.request_counts(start, end)  # Azure Monitor first, so the DB is woken only if needed

    def work(repo) -> List[Alert]:
        rows = reconcile(
            repo.captured_counts(first, today), gateway, days,
            floor=settings.capture_rate_floor, min_gateway_requests=settings.reconcile_min_gateway_requests,
        )
        repo.upsert_reconciliation(rows)
        summary.rows_written = len(rows)
        summary.requests = sum(r.captured_requests for r in rows)
        summary.malformed = sum(max(0, r.gateway_requests - r.captured_requests) for r in rows if r.status in ("ok", "gap"))
        new = repo.insert_new_alerts(reconciliation_alerts(rows, settings.capture_rate_floor))
        summary.alerts_created = len(new)
        return new

    new_alerts = db.run(work)
    if new_alerts and notifier.send(new_alerts):
        db.run(lambda repo: repo.mark_notified([a.key for a in new_alerts]))
    summary.duration_seconds = time.monotonic() - t0
    db.run(lambda repo: repo.record_run(summary, started))
    return summary
