"""Hot path: validate, de-duplicate and aggregate LLM telemetry pushed by APIM in near real time.

Two ways in (both end up in `ingest_events`):
  * Event Hub trigger  - APIM `log-to-eventhub` -> hub -> Function (cardinality "many", the high-volume path)
  * POST /api/ingest   - any client / gateway that can send JSON (handy for tests and non-APIM sources)

Design choices that matter at ~15 M events/month:
  * Events are aggregated in memory per (hour, department, model) *before* touching SQL, so a batch of
    1,000 events costs one MERGE per group, not 1,000 inserts.
  * Every batch gets a deterministic id (hash of its request ids). Event Hub redelivers batches after a
    crash; the id makes the additive write idempotent.
  * Bad events are never dropped silently: they go to a dead-letter table with a reason, and count
    against the capture-rate metric.
The authoritative daily numbers still come from the Log Analytics sync; this path gives "today so far".
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .pricing import PriceBook, compute_cost, normalize_model

MAX_FIELD = 100
MAX_TOKENS_PER_EVENT = 5_000_000  # sanity bound; a larger value is a unit/parsing bug, not real usage


class EventRejected(ValueError):
    """Raised by parse_event; the message becomes the dead-letter reason."""


@dataclass(frozen=True)
class TelemetryEvent:
    request_id: str
    occurred_at: datetime  # timezone-aware UTC
    department_id: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: Optional[int]


@dataclass(frozen=True)
class RejectedEvent:
    reason: str
    raw: str  # truncated JSON, for debugging


@dataclass(frozen=True)
class HourlyAggregate:
    bucket: datetime  # start of the UTC hour (timezone-aware)
    department_id: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    request_count: int
    p95_total_tokens: Optional[int]
    avg_latency_ms: Optional[int]
    p95_latency_ms: Optional[int]


@dataclass
class IngestResult:
    batch_id: str = ""
    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: List[RejectedEvent] = field(default_factory=list)
    aggregates: List[HourlyAggregate] = field(default_factory=list)
    replayed: bool = False  # True when the batch id was already stored (safe no-op)

    @property
    def capture_rate(self) -> Optional[float]:
        """Share of received events that were usable (duplicates are not counted as failures)."""
        denom = self.received - self.duplicates
        return None if denom <= 0 else self.accepted / denom


# --------------------------------------------------------------------------- parsing

def _get(raw: Dict[str, Any], *names: str) -> Any:
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None


def _to_int(value: Any, field_name: str, *, required: bool) -> Optional[int]:
    if value is None or value == "":
        if required:
            raise EventRejected(f"missing {field_name}")
        return None
    if isinstance(value, bool):
        raise EventRejected(f"{field_name} is not a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise EventRejected(f"{field_name} is not a number") from None
    if math.isnan(number) or math.isinf(number) or number != int(number):
        raise EventRejected(f"{field_name} is not a whole number")
    n = int(number)
    if n < 0:
        # APIM emits -1 when the response had no usage block: that is a capture failure, not zero usage.
        raise EventRejected(f"{field_name} is negative (usage missing in response)")
    if n > MAX_TOKENS_PER_EVENT:
        raise EventRejected(f"{field_name} is implausibly large")
    return n


def _parse_time(value: Any, now: datetime) -> datetime:
    if value in (None, ""):
        return now  # no source timestamp: fall back to receipt time
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        raise EventRejected("timestamp is not ISO-8601") from None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def parse_event(
    raw: Any, *, now: datetime, max_age: timedelta = timedelta(days=7), max_future: timedelta = timedelta(minutes=10)
) -> TelemetryEvent:
    if not isinstance(raw, dict):
        raise EventRejected("event is not a JSON object")
    request_id = str(_get(raw, "RequestId", "request_id", "id") or "").strip()
    if not request_id:
        raise EventRejected("missing RequestId")
    when = _parse_time(_get(raw, "Timestamp", "timestamp", "time"), now)
    if when < now - max_age:
        raise EventRejected("timestamp too old")
    if when > now + max_future:
        raise EventRejected("timestamp in the future")
    prompt = _to_int(_get(raw, "PromptTokens", "prompt_tokens"), "PromptTokens", required=True)
    completion = _to_int(_get(raw, "CompletionTokens", "completion_tokens"), "CompletionTokens", required=True)
    latency = _to_int(_get(raw, "LatencyMs", "latency_ms"), "LatencyMs", required=False)
    dept = str(_get(raw, "DepartmentId", "department_id") or "").strip()[:MAX_FIELD] or "Unknown"
    model = normalize_model(str(_get(raw, "ModelName", "model_name", "model") or ""))[:MAX_FIELD]
    return TelemetryEvent(request_id[:200], when, dept, model, prompt, completion, latency)


def unpack_payload(payload: Any) -> List[Any]:
    """Accept a JSON object, a list of objects, or {"events": [...]} (what a batching sender produces)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return payload["events"]
    return [payload]


def unpack_bodies(bodies: Iterable[Any]) -> List[Any]:
    """Event Hub delivers one body per message; each may itself hold one event or an array."""
    items: List[Any] = []
    for body in bodies:
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", errors="replace")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                items.append({"__unparseable__": body[:300]})
                continue
        items.extend(unpack_payload(body))
    return items


# --------------------------------------------------------------------------- aggregation

def percentile(values: Sequence[int], pct: float) -> Optional[int]:
    """Nearest-rank percentile (matches how an operator would reason about 'p95')."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def hour_bucket(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def aggregate_hourly(events: Sequence[TelemetryEvent]) -> List[HourlyAggregate]:
    groups: Dict[Tuple[datetime, str, str], List[TelemetryEvent]] = defaultdict(list)
    for e in events:
        groups[(hour_bucket(e.occurred_at), e.department_id, e.model_name)].append(e)
    out: List[HourlyAggregate] = []
    for (bucket, dept, model), evs in sorted(groups.items()):
        latencies = [e.latency_ms for e in evs if e.latency_ms is not None]
        out.append(HourlyAggregate(
            bucket=bucket, department_id=dept, model_name=model,
            prompt_tokens=sum(e.prompt_tokens for e in evs),
            completion_tokens=sum(e.completion_tokens for e in evs),
            request_count=len(evs),
            p95_total_tokens=percentile([e.prompt_tokens + e.completion_tokens for e in evs], 95),
            avg_latency_ms=round(sum(latencies) / len(latencies)) if latencies else None,
            p95_latency_ms=percentile(latencies, 95),
        ))
    return out


def batch_id_for(request_ids: Iterable[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(request_ids)).encode("utf-8")).hexdigest()
    return digest[:32]


def process_events(
    items: Sequence[Any],
    *,
    now: Optional[datetime] = None,
    max_age_days: int = 7,
    max_future_minutes: int = 10,
) -> IngestResult:
    """Pure function: raw items in, validated + de-duplicated + aggregated result out."""
    now = now or datetime.now(timezone.utc)
    result = IngestResult(received=len(items))
    seen: set = set()
    good: List[TelemetryEvent] = []
    for raw in items:
        try:
            event = parse_event(
                raw, now=now, max_age=timedelta(days=max_age_days), max_future=timedelta(minutes=max_future_minutes)
            )
        except EventRejected as exc:
            result.rejected.append(RejectedEvent(str(exc), json.dumps(raw, default=str)[:500]))
            continue
        if event.request_id in seen:
            result.duplicates += 1
            continue
        seen.add(event.request_id)
        good.append(event)
    result.accepted = len(good)
    result.aggregates = aggregate_hourly(good)
    result.batch_id = batch_id_for(e.request_id for e in good) if good else ""
    return result


def price_aggregates(
    aggregates: Sequence[HourlyAggregate], book: PriceBook
) -> List[Tuple[HourlyAggregate, Decimal, bool]]:
    priced: List[Tuple[HourlyAggregate, Decimal, bool]] = []
    for a in aggregates:
        version = book.lookup(a.model_name, a.bucket.date())
        if version is None:
            priced.append((a, Decimal("0"), False))
        else:
            priced.append((a, compute_cost(a.prompt_tokens, a.completion_tokens, version), True))
    return priced


def ingest_events(
    db, items: Sequence[Any], *, now: Optional[datetime] = None, max_age_days: int = 7, max_future_minutes: int = 10
) -> IngestResult:
    """Validate, then write one idempotent unit of work to SQL."""
    result = process_events(items, now=now, max_age_days=max_age_days, max_future_minutes=max_future_minutes)
    if not result.accepted and not result.rejected:
        return result

    def work(repo) -> None:
        if result.accepted and not repo.claim_batch(result.batch_id, result.accepted):
            result.replayed = True  # same batch delivered twice: nothing to add, nothing to dead-letter again
            return
        if result.aggregates:
            repo.add_hourly_usage(price_aggregates(result.aggregates, repo.load_price_book()))
        if result.rejected:
            repo.save_dead_letters(result.batch_id or "no-valid-events", result.rejected)

    db.run(work)
    return result
