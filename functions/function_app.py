"""Azure Functions entry points (Python v2 programming model).

Everything heavy lives in the `finops` package; the functions only wire it together.
"""
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import List

import azure.functions as func

app = func.FunctionApp()


def _deps():
    # Built lazily so importing this module (indexing) never needs the DB or network.
    from finops.config import get_settings
    from finops.db import Database
    from finops.kql import LogAnalyticsSource
    from finops.notify import WebhookNotifier

    s = get_settings()
    return s, LogAnalyticsSource(s.workspace_id), Database(s), WebhookNotifier(s.alert_webhook_url)


def _db():
    """Settings + database only (the hot path never needs Log Analytics or the webhook)."""
    from finops.config import get_settings
    from finops.db import Database

    s = get_settings()
    return s, Database(s)


def _json(payload, status=200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload, default=str), status_code=status, mimetype="application/json")


@app.timer_trigger(schedule="0 0 2 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def daily_cost_sync(timer: func.TimerRequest) -> None:
    """02:00 UTC: re-aggregate the last LOOKBACK_DAYS complete days, price them, raise alerts."""
    from finops.pipeline import run_daily_sync

    settings, source, db, notifier = _deps()
    run_daily_sync(settings, source, db, notifier)


@app.timer_trigger(schedule="0 0 3 * * 1", arg_name="timer", run_on_startup=False, use_monitor=True)
def weekly_routing_review(timer: func.TimerRequest) -> None:
    """Mondays 03:00 UTC: routing advice, waste detection, impact measurement, hot-path cleanup.

    Weekly keeps vCore-seconds low. Steps are independent: one failing must not skip the others.
    """
    from finops.impact import run_impact_review
    from finops.pipeline import run_efficiency_review, run_maintenance, run_routing_review

    settings, _, db, _ = _deps()
    failures = []
    for name, step in (
        ("routing", lambda: run_routing_review(settings, db)),
        ("efficiency", lambda: run_efficiency_review(settings, db)),
        ("impact", lambda: run_impact_review(settings, db)),
        ("maintenance", lambda: run_maintenance(settings, db)),
    ):
        try:
            step()
        except Exception:  # noqa: BLE001
            logging.exception("Weekly step '%s' failed", name)
            failures.append(name)
    if failures:
        raise RuntimeError(f"Weekly review steps failed: {', '.join(failures)}")


@app.timer_trigger(schedule="0 30 2 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def capture_reconciliation(timer: func.TimerRequest) -> None:
    """02:30 UTC: compare captured telemetry with APIM's own request counter (the honest capture rate)."""
    from finops.reconcile import ApimMetricsSource, run_capture_reconciliation

    settings, _, db, notifier = _deps()
    if not settings.apim_resource_id:
        logging.warning("APIM_RESOURCE_ID is not set; skipping capture reconciliation.")
        return
    metrics = ApimMetricsSource(settings.apim_resource_id, settings.apim_metric_filter)
    run_capture_reconciliation(settings, metrics, db, notifier)


# --- real-time ingest ----------------------------------------------------------------------
# Retries are safe: every batch has a deterministic id, so a redelivered batch is a no-op.
@app.event_hub_message_trigger(
    arg_name="events", event_hub_name="%EVENTHUB_NAME%", connection="EVENTHUB_CONNECTION",
    consumer_group="$Default", cardinality="many",
)
@app.retry(strategy="exponential_backoff", max_retry_count="5", minimum_interval="00:00:10", maximum_interval="00:10:00")
def ingest_eventhub(events: List[func.EventHubEvent]) -> None:
    """APIM `log-to-eventhub` -> Event Hub -> here. One DB unit of work per delivered batch."""
    from finops.ingest import ingest_events, unpack_bodies

    settings, db = _db()
    result = ingest_events(
        db, unpack_bodies(e.get_body() for e in events),
        max_age_days=settings.ingest_max_age_days, max_future_minutes=settings.ingest_max_future_minutes,
    )
    logging.info(
        "Ingested batch %s: received=%d accepted=%d duplicates=%d rejected=%d replayed=%s",
        result.batch_id[:8], result.received, result.accepted, result.duplicates, len(result.rejected), result.replayed,
    )


@app.route(route="ingest", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def ingest(req: func.HttpRequest) -> func.HttpResponse:
    """POST one event, an array, or {"events": [...]} using header `x-functions-key`.

    Event fields: RequestId, Timestamp (ISO-8601, optional), DepartmentId, ModelName, PromptTokens,
    CompletionTokens, LatencyMs (optional). Returns 202 with accepted/duplicate/rejected counts.
    """
    from finops.ingest import ingest_events, unpack_payload

    settings, db = _db()
    body = req.get_body()
    if len(body) > settings.ingest_max_body_bytes:
        return _json({"error": f"Body larger than {settings.ingest_max_body_bytes} bytes."}, 413)
    try:
        items = unpack_payload(json.loads(body))
    except ValueError:
        return _json({"error": "Body must be valid JSON."}, 400)

    result = ingest_events(
        db, items, max_age_days=settings.ingest_max_age_days, max_future_minutes=settings.ingest_max_future_minutes
    )
    payload = {
        "batch_id": result.batch_id, "received": result.received, "accepted": result.accepted,
        "duplicates": result.duplicates, "rejected": len(result.rejected), "replayed": result.replayed,
        "reject_reasons": sorted({r.reason for r in result.rejected})[:10],
    }
    return _json(payload, 202 if result.accepted or result.replayed else 422)


@app.route(route="backfill", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def backfill(req: func.HttpRequest) -> func.HttpResponse:
    """POST {"from": "2026-10-01", "to": "2026-10-20"} - ingest a date range (inclusive).

    Log Analytics keeps ~31 days by default, so the range is capped at 31 days.
    Analytics/alerts are skipped so history doesn't trigger notification storms.
    """
    from finops.pipeline import run_daily_sync

    try:
        body = req.get_json()
        start, end = date.fromisoformat(body["from"]), date.fromisoformat(body["to"])
    except Exception:  # noqa: BLE001
        return _json({"error": 'Body must be JSON: {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}'}, 400)

    today = datetime.now(timezone.utc).date()
    if start > end or end >= today or (end - start).days >= 31:
        return _json({"error": "Need from <= to < today (UTC) and a range of at most 31 days."}, 400)

    settings, source, db, notifier = _deps()
    summary = run_daily_sync(
        settings, source, db, notifier,
        today=end + timedelta(days=1), lookback_days=(end - start).days + 1,
        run_type="backfill", analytics=False,
    )
    return _json(asdict(summary) | {"capture_rate": summary.capture_rate})


@app.route(route="status", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def status(req: func.HttpRequest) -> func.HttpResponse:
    """Last pipeline runs. Note: this wakes the serverless DB (costs a few vCore-seconds)."""
    settings, _, db, _ = _deps()
    return _json(db.run(lambda repo: repo.recent_runs(10)))


@app.route(route="report", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def report(req: func.HttpRequest) -> func.HttpResponse:
    """GET /api/report?days=30 - showback by department/model, capture rate, realised savings and
    latency change, open opportunities, and spend so far today. Wakes the serverless DB."""
    from finops.report import build_report

    try:
        days = max(1, min(int(req.params.get("days", "30")), 365))
    except ValueError:
        return _json({"error": "days must be an integer"}, 400)
    settings, db = _db()
    today = datetime.now(timezone.utc).date()
    return _json(db.run(lambda repo: build_report(repo, today, days)))


@app.route(route="recommendations/status", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def recommendation_status(req: func.HttpRequest) -> func.HttpResponse:
    """POST {"department": "...", "current_model": "...", "suggested_model": "...",
              "status": "accepted|dismissed|open", "accepted_on": "YYYY-MM-DD" (optional, backdates)}

    Marking a recommendation `accepted` starts the before/after impact measurement.
    """
    try:
        b = req.get_json()
        status = str(b["status"])
        accepted_on = date.fromisoformat(b["accepted_on"]) if b.get("accepted_on") else None
        dept, cur, sug = str(b["department"]), str(b["current_model"]), str(b["suggested_model"])
    except Exception:  # noqa: BLE001
        return _json({"error": "Body needs department, current_model, suggested_model, status (and optional accepted_on)."}, 400)
    if status not in ("accepted", "dismissed", "open"):
        return _json({"error": "status must be accepted, dismissed or open"}, 400)
    if accepted_on and accepted_on > datetime.now(timezone.utc).date():
        return _json({"error": "accepted_on cannot be in the future"}, 400)

    _, db = _db()
    found = db.run(lambda repo: repo.set_recommendation_status(dept, cur, sug, status, accepted_on))
    return _json({"updated": found}, 200 if found else 404)
