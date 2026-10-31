"""Plain data containers shared across the pipeline (no Azure dependencies)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class UsageRow:
    """One (day, department, model) aggregate coming out of Log Analytics."""

    usage_date: date
    department_id: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    request_count: int
    malformed_count: int = 0
    p95_total_tokens: Optional[int] = None
    avg_latency_ms: Optional[int] = None
    p95_latency_ms: Optional[int] = None


@dataclass(frozen=True)
class PriceVersion:
    """A price that applies from `effective_from` until the next version."""

    effective_from: date
    prompt_per_1k: Decimal
    completion_per_1k: Decimal
    fallback_model: Optional[str] = None


@dataclass(frozen=True)
class PricedUsage:
    usage: UsageRow
    cost: Decimal
    priced: bool  # False -> model had no price on that date; cost is 0


@dataclass(frozen=True)
class Alert:
    key: str  # dedupe key; one notification per key, ever
    type: str  # anomaly | budget_warning | budget_critical | budget_forecast | unpriced_model | capture_rate
    severity: str  # info | warning | critical
    message: str
    department_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelStats:
    """Aggregated usage of one (department, model) over the routing window."""

    department_id: str
    model_name: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    max_p95_total_tokens: Optional[int]
    avg_latency_ms: Optional[float]
    malformed_count: int = 0


@dataclass(frozen=True)
class RoutingRecommendation:
    department_id: str
    current_model: str
    suggested_model: str
    requests: int
    avg_prompt_tokens: int
    avg_completion_tokens: int
    p95_total_tokens: int
    window_cost: Decimal
    est_monthly_savings: Decimal
    savings_pct: float
    confidence: str  # medium | high
    latency_delta_pct: Optional[float] = None  # negative = suggested model is faster


@dataclass
class RunSummary:
    run_type: str
    status: str = "success"  # success | partial | failed
    window_start: Optional[date] = None
    window_end: Optional[date] = None  # exclusive
    rows_written: int = 0
    requests: int = 0
    malformed: int = 0
    unpriced_rows: int = 0
    alerts_created: int = 0
    recommendations: int = 0
    duration_seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)

    @property
    def capture_rate(self) -> Optional[float]:
        total = self.requests + self.malformed
        return None if total == 0 else self.requests / total


@dataclass(frozen=True)
class CostLine:
    """Cost of one (department, model) pair over a reporting window (input to showback)."""

    department_id: str
    model_name: str
    cost: Decimal
    requests: int
    total_tokens: int


@dataclass(frozen=True)
class Inefficiency:
    """A concrete, costed waste pattern found in the usage data."""

    department_id: str
    model_name: str
    kind: str  # prompt_bloat | uncapped_completion | telemetry_gap
    severity: str  # info | warning
    requests: int
    est_monthly_waste: Decimal
    detail: str
    suggestion: str


@dataclass(frozen=True)
class WindowTotals:
    """Usage of one department over a date window (all models together)."""

    requests: int
    cost: Decimal
    avg_latency_ms: Optional[float]


@dataclass(frozen=True)
class AcceptedRecommendation:
    department_id: str
    current_model: str
    suggested_model: str
    accepted_on: date


@dataclass(frozen=True)
class ImpactResult:
    """Before/after comparison for one accepted routing recommendation."""

    department_id: str
    current_model: str
    suggested_model: str
    accepted_on: date
    status: str  # measured | insufficient_data
    before_requests: int
    after_requests: int
    before_cost_per_request: Optional[Decimal]
    after_cost_per_request: Optional[Decimal]
    avoided_cost: Decimal
    cost_reduction_pct: Optional[float]
    before_latency_ms: Optional[float]
    after_latency_ms: Optional[float]
    latency_change_pct: Optional[float]  # negative = faster


@dataclass(frozen=True)
class ReconciliationRow:
    """Captured telemetry vs. the gateway's own request count for one UTC day."""

    usage_date: date
    gateway_requests: int
    captured_requests: int
    capture_rate: Optional[float]
    status: str  # ok | gap | over_capture | no_gateway_data
