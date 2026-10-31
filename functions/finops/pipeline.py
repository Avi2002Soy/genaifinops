"""Orchestration. Takes its collaborators as arguments so it can be unit-tested with fakes."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from .anomaly import detect_anomalies
from .budget import evaluate_budgets
from .efficiency import detect_inefficiencies
from .config import Settings
from .models import Alert, PricedUsage, RunSummary, UsageRow
from .notify import WebhookNotifier
from .pricing import PriceBook, consolidate, price_usage
from .routing import recommend_routing


class UsageSource(Protocol):
    def fetch(self, start: datetime, end: datetime) -> List[UsageRow]: ...


def utc_midnight(d: date) -> datetime:
    return datetime.combine(d, dtime.min, tzinfo=timezone.utc)


def day_window(today: date, lookback_days: int) -> Tuple[datetime, datetime]:
    """[start, end) covering the last `lookback_days` COMPLETE UTC days.

    The original code queried now-24h..now, which splits one calendar day across two
    runs and then double-inserts it.
    """
    return utc_midnight(today - timedelta(days=lookback_days)), utc_midnight(today)


def build_alerts(
    settings: Settings,
    repo,
    priced: Sequence[PricedUsage],
    summary: RunSummary,
    as_of: date,
) -> List[Alert]:
    alerts: List[Alert] = []
    history = repo.daily_costs(as_of - timedelta(days=settings.history_days - 1))

    for a in detect_anomalies(
        history, as_of,
        window_days=settings.anomaly_window_days,
        min_history_days=settings.anomaly_min_history_days,
        threshold=settings.anomaly_score_threshold,
        min_abs_delta=settings.anomaly_min_abs_delta_usd,
    ):
        ratio = f" ({a.ratio:.1f}x normal)" if a.ratio else ""
        alerts.append(Alert(
            f"anomaly:{a.department_id}:{a.usage_date}", "anomaly", "warning",
            f"Cost spike for {a.department_id} on {a.usage_date}: ${a.observed:.2f} vs ~${a.expected:.2f} expected{ratio}.",
            a.department_id,
            {"observed": str(a.observed), "expected": str(a.expected), "score": a.score},
        ))

    alerts += evaluate_budgets(repo.budgets(), history, as_of, warn_pct=settings.budget_warn_pct)

    for model in sorted({p.usage.model_name for p in priced if not p.priced}):
        alerts.append(Alert(
            f"unpriced_model:{model}", "unpriced_model", "warning",
            f"Model '{model}' has usage but no price in ModelPricing; its cost is being recorded as $0.",
            None, {"model": model},
        ))

    total = summary.requests + summary.malformed
    if total >= settings.capture_min_events and summary.capture_rate is not None \
            and summary.capture_rate < settings.capture_rate_floor:
        alerts.append(Alert(
            f"capture_rate:{as_of}", "capture_rate", "warning",
            f"Telemetry parse rate for {as_of} was {summary.capture_rate:.3%} "
            f"({summary.malformed} of {total} events lacked usable token counts).",
            None, {"malformed": summary.malformed, "total": total},
        ))
    return alerts


def run_daily_sync(
    settings: Settings,
    source: UsageSource,
    db,
    notifier: WebhookNotifier,
    *,
    today: Optional[date] = None,
    lookback_days: Optional[int] = None,
    run_type: str = "daily",
    analytics: bool = True,
) -> RunSummary:
    t0 = time.monotonic()
    started = datetime.now(timezone.utc)
    today = today or started.date()
    start, end = day_window(today, lookback_days or settings.lookback_days)
    summary = RunSummary(run_type=run_type, window_start=start.date(), window_end=end.date())

    try:
        # Query Log Analytics first so the serverless DB is only woken once we have data.
        rows = consolidate(source.fetch(start, end))
        summary.requests = sum(r.request_count for r in rows)
        summary.malformed = sum(r.malformed_count for r in rows)

        def work(repo) -> List[Alert]:
            book: PriceBook = repo.load_price_book()
            priced = price_usage(rows, book)
            summary.rows_written = repo.upsert_usage(priced)
            summary.unpriced_rows = sum(1 for p in priced if not p.priced)
            repo.commit()  # ingestion is durable even if analytics below fails
            if not analytics:
                return []
            try:
                alerts = build_alerts(settings, repo, priced, summary, end.date() - timedelta(days=1))
                new = repo.insert_new_alerts(alerts)
                summary.alerts_created = len(new)
                return new
            except Exception as exc:  # noqa: BLE001
                logging.exception("Analytics step failed; ingestion already committed")
                summary.status = "partial"
                summary.warnings.append(f"analytics failed: {exc}")
                return []

        new_alerts: List[Alert] = db.run(work)

        if new_alerts and notifier.send(new_alerts):
            db.run(lambda repo: repo.mark_notified([a.key for a in new_alerts]))
    except Exception as exc:
        summary.status = "failed"
        summary.warnings.append(str(exc)[:500])
        summary.duration_seconds = time.monotonic() - t0
        try:  # best effort: the DB may be the thing that is down
            db.run(lambda repo: repo.record_run(summary, started))
        except Exception:  # noqa: BLE001
            logging.exception("Could not record failed run")
        raise

    summary.duration_seconds = time.monotonic() - t0
    db.run(lambda repo: repo.record_run(summary, started))
    logging.info("Run complete: %s", summary)
    return summary


def run_routing_review(
    settings: Settings, db, *, today: Optional[date] = None
) -> RunSummary:
    t0 = time.monotonic()
    started = datetime.now(timezone.utc)
    as_of = (today or started.date()) - timedelta(days=1)
    summary = RunSummary(run_type="routing", window_end=as_of + timedelta(days=1))
    summary.window_start = as_of - timedelta(days=settings.routing_window_days - 1)

    def work(repo) -> int:
        book = repo.load_price_book()
        stats = repo.model_stats(summary.window_start)
        recs = recommend_routing(
            stats, book, as_of,
            window_days=settings.routing_window_days,
            min_requests=settings.routing_min_requests,
            max_avg_prompt_tokens=settings.routing_max_avg_prompt_tokens,
            max_p95_total_tokens=settings.routing_max_p95_total_tokens,
            min_monthly_savings=settings.routing_min_monthly_savings_usd,
        )
        # 5-minute margin absorbs clock skew between the Function host and the SQL server.
        repo.save_recommendations(recs, (started - timedelta(minutes=5)).replace(tzinfo=None))
        return len(recs)

    summary.recommendations = db.run(work)
    summary.duration_seconds = time.monotonic() - t0
    db.run(lambda repo: repo.record_run(summary, started))
    return summary


def run_efficiency_review(settings: Settings, db, *, today: Optional[date] = None) -> RunSummary:
    """Weekly: find costed waste patterns (prompt bloat, uncapped output, telemetry gaps)."""
    t0 = time.monotonic()
    started = datetime.now(timezone.utc)
    as_of = (today or started.date()) - timedelta(days=1)
    summary = RunSummary(run_type="efficiency", window_end=as_of + timedelta(days=1))
    summary.window_start = as_of - timedelta(days=settings.eff_window_days - 1)

    def work(repo) -> int:
        items = detect_inefficiencies(
            repo.model_stats(summary.window_start), repo.load_price_book(), as_of,
            window_days=settings.eff_window_days, min_requests=settings.eff_min_requests,
            bloat_avg_tokens=settings.eff_prompt_bloat_avg_tokens, bloat_ratio=settings.eff_prompt_bloat_ratio,
            target_prompt_tokens=settings.eff_prompt_target_tokens,
            uncapped_avg_tokens=settings.eff_uncapped_completion_avg_tokens,
            cap_saving_pct=settings.eff_completion_cap_saving_pct, gap_ratio=settings.eff_telemetry_gap_ratio,
            min_monthly_waste=settings.eff_min_monthly_waste_usd,
        )
        repo.save_inefficiencies(items, (started - timedelta(minutes=5)).replace(tzinfo=None))
        return len(items)

    summary.recommendations = db.run(work)
    summary.duration_seconds = time.monotonic() - t0
    db.run(lambda repo: repo.record_run(summary, started))
    return summary


def run_maintenance(settings: Settings, db, *, now: Optional[datetime] = None) -> RunSummary:
    """Weekly: purge hot-path rows (hourly usage, batch ids, dead letters) past the retention window.

    The daily table is authoritative and kept forever; the hourly table only exists for "today so far".
    """
    t0 = time.monotonic()
    started = now or datetime.now(timezone.utc)
    cutoff = (started - timedelta(days=settings.ingest_retention_days)).replace(tzinfo=None)
    summary = RunSummary(run_type="maintenance")
    db.run(lambda repo: repo.purge_old_ingest(cutoff))
    summary.duration_seconds = time.monotonic() - t0
    db.run(lambda repo: repo.record_run(summary, started))
    return summary
