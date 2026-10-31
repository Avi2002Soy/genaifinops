"""Model-routing recommendations: find workloads that don't need the model they use.

Heuristic (deliberately conservative): a (department, model) pair is a candidate
to move to the model's configured `FallbackModel` when it has enough traffic and
its requests are short - small average prompts and a small p95 of total tokens.
Output is a recommendation for a human to review, never an automatic switch.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from .models import ModelStats, RoutingRecommendation
from .pricing import PriceBook, compute_cost, normalize_model


def latency_by_model(stats: Sequence[ModelStats]) -> Dict[str, float]:
    """Request-weighted mean latency per model across all departments."""
    num: Dict[str, float] = {}
    den: Dict[str, int] = {}
    for s in stats:
        if s.avg_latency_ms is None or s.requests <= 0:
            continue
        num[s.model_name] = num.get(s.model_name, 0.0) + s.avg_latency_ms * s.requests
        den[s.model_name] = den.get(s.model_name, 0) + s.requests
    return {m: num[m] / den[m] for m in num}


def recommend_routing(
    stats: Sequence[ModelStats],
    book: PriceBook,
    as_of: date,
    *,
    window_days: int = 30,
    min_requests: int = 200,
    max_avg_prompt_tokens: int = 800,
    max_p95_total_tokens: int = 2000,
    min_monthly_savings: Decimal = Decimal("5"),
) -> List[RoutingRecommendation]:
    latencies = latency_by_model(stats)
    scale = Decimal(30) / Decimal(window_days)
    recs: List[RoutingRecommendation] = []
    for s in stats:
        if s.requests < min_requests or s.max_p95_total_tokens is None:
            continue
        current = book.lookup(s.model_name, as_of)
        if current is None or not current.fallback_model:
            continue
        alt = book.lookup(current.fallback_model, as_of)
        if alt is None:
            continue
        avg_prompt = s.prompt_tokens // s.requests
        avg_completion = s.completion_tokens // s.requests
        if avg_prompt > max_avg_prompt_tokens or s.max_p95_total_tokens > max_p95_total_tokens:
            continue
        cost_now = compute_cost(s.prompt_tokens, s.completion_tokens, current)
        cost_alt = compute_cost(s.prompt_tokens, s.completion_tokens, alt)
        savings = (cost_now - cost_alt) * scale
        if savings < min_monthly_savings or cost_now <= 0:
            continue
        fb_key_name = normalize_model(current.fallback_model)
        confident = s.requests >= 5 * min_requests and s.max_p95_total_tokens <= max_p95_total_tokens / 2
        delta: Optional[float] = None
        fb_key = normalize_model(current.fallback_model)
        if fb_key in latencies and s.model_name in latencies and latencies[s.model_name] > 0:
            delta = round((latencies[fb_key] - latencies[s.model_name]) / latencies[s.model_name] * 100, 1)
        recs.append(RoutingRecommendation(
            department_id=s.department_id,
            current_model=s.model_name,
            suggested_model=fb_key_name,
            requests=s.requests,
            avg_prompt_tokens=avg_prompt,
            avg_completion_tokens=avg_completion,
            p95_total_tokens=s.max_p95_total_tokens,
            window_cost=cost_now,
            est_monthly_savings=savings.quantize(Decimal("0.01")),
            savings_pct=round(float((cost_now - cost_alt) / cost_now * 100), 1),
            confidence="high" if confident else "medium",
            latency_delta_pct=delta,
        ))
    return sorted(recs, key=lambda r: r.est_monthly_savings, reverse=True)
