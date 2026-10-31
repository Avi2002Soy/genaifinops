"""Did accepting a routing recommendation actually help?  Measures realised cost AND latency change.

For every recommendation marked `accepted` we compare the department's traffic before and after the
acceptance date, using *rates* (cost per request, avg latency) so the two windows may differ in length
or volume.  Output feeds `dbo.RoutingImpact` and the Power BI "Savings & latency" page.

Honest caveats (also in the README): this is a before/after comparison, not a controlled experiment.
A change in prompt mix, seasonality or a price change inside the windows can move the numbers.
Windows with too little traffic are stored as `insufficient_data`, never as a made-up percentage.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Settings
from .models import AcceptedRecommendation, ImpactResult, RunSummary, WindowTotals

_SIX_DP = Decimal("0.000001")
_CENT = Decimal("0.01")


def impact_windows(
    accepted_on: date, as_of: date, window_days: int, min_days_after: int
) -> Optional[Tuple[date, date, date, date]]:
    """(before_start, before_end, after_start, after_end), end dates exclusive; None if too early to judge.

    `as_of` is the last complete day. The after-window starts the day *after* acceptance (the switch
    day itself is a mix of old and new behaviour) and is cut off at `as_of`.
    """
    after_start = accepted_on + timedelta(days=1)
    after_end = min(after_start + timedelta(days=window_days), as_of + timedelta(days=1))
    if (after_end - after_start).days < min_days_after:
        return None
    return accepted_on - timedelta(days=window_days), accepted_on, after_start, after_end


def _cost_per_request(t: WindowTotals) -> Optional[Decimal]:
    return None if t.requests <= 0 else (t.cost / t.requests).quantize(_SIX_DP)


def measure_impact(
    rec: AcceptedRecommendation, before: WindowTotals, after: WindowTotals, *, min_requests: int = 200
) -> ImpactResult:
    b_cpr, a_cpr = _cost_per_request(before), _cost_per_request(after)
    enough = before.requests >= min_requests and after.requests >= min_requests and b_cpr and b_cpr > 0 and a_cpr is not None
    if not enough:
        return ImpactResult(
            rec.department_id, rec.current_model, rec.suggested_model, rec.accepted_on, "insufficient_data",
            before.requests, after.requests, b_cpr, a_cpr, Decimal("0"), None,
            before.avg_latency_ms, after.avg_latency_ms, None,
        )
    avoided = ((b_cpr - a_cpr) * after.requests).quantize(_CENT)
    reduction = round(float((b_cpr - a_cpr) / b_cpr * 100), 1)
    lat_change: Optional[float] = None
    if before.avg_latency_ms and after.avg_latency_ms is not None:
        lat_change = round((after.avg_latency_ms - before.avg_latency_ms) / before.avg_latency_ms * 100, 1)
    return ImpactResult(
        rec.department_id, rec.current_model, rec.suggested_model, rec.accepted_on, "measured",
        before.requests, after.requests, b_cpr, a_cpr, avoided, reduction,
        before.avg_latency_ms, after.avg_latency_ms, lat_change,
    )


def summarize_impact(results: Sequence[ImpactResult]) -> Dict[str, object]:
    """Program-level numbers: spend reduction vs. what the same traffic would have cost, and latency change."""
    measured = [r for r in results if r.status == "measured"]
    avoided = sum((r.avoided_cost for r in measured), Decimal("0"))
    actual = sum((r.after_cost_per_request * r.after_requests for r in measured if r.after_cost_per_request is not None), Decimal("0"))
    baseline = actual + avoided
    lat_num = lat_den = 0.0
    for r in measured:
        if r.latency_change_pct is not None:
            lat_num += r.latency_change_pct * r.after_requests
            lat_den += r.after_requests
    return {
        "recommendations_measured": len(measured),
        "recommendations_insufficient_data": len(results) - len(measured),
        "avoided_cost_usd": avoided.quantize(_CENT),
        "spend_reduction_pct": round(float(avoided / baseline * 100), 1) if baseline > 0 else None,
        "avg_latency_change_pct": round(lat_num / lat_den, 1) if lat_den else None,  # negative = faster
    }


def run_impact_review(settings: Settings, db, *, today: Optional[date] = None) -> RunSummary:
    t0 = time.monotonic()
    started = datetime.now(timezone.utc)
    as_of = (today or started.date()) - timedelta(days=1)
    summary = RunSummary(run_type="impact", window_end=as_of + timedelta(days=1))

    def work(repo) -> int:
        results: List[ImpactResult] = []
        for rec in repo.accepted_recommendations():
            w = impact_windows(rec.accepted_on, as_of, settings.impact_window_days, settings.impact_min_days_after)
            if w is None:
                continue
            b_start, b_end, a_start, a_end = w
            results.append(measure_impact(
                rec,
                repo.window_totals(rec.department_id, b_start, b_end),
                repo.window_totals(rec.department_id, a_start, a_end),
                min_requests=settings.impact_min_requests,
            ))
        repo.save_impact(results)
        summary.rows_written = len(results)
        return sum(1 for r in results if r.status == "measured")

    summary.recommendations = db.run(work)
    summary.duration_seconds = time.monotonic() - t0
    db.run(lambda repo: repo.record_run(summary, started))
    return summary
