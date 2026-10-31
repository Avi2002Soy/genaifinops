"""Inefficiency detection: turn usage statistics into costed, actionable waste findings.

Three patterns, chosen because each is visible from token counts alone (no prompt content is stored):

  prompt_bloat         huge, input-heavy prompts (re-sent context, over-stuffed RAG). Fix: trim / cache / retrieve less.
  uncapped_completion  very long answers on average. Fix: set max_tokens, ask for concise output.
  telemetry_gap        many calls came back without usable token counts, so spend is unattributed.

Savings figures are *estimates* built from explicit assumptions (target prompt size, % saved by a cap),
and every finding says so. Routing (cheaper model) is handled separately in routing.py.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import List, Sequence

from .models import Inefficiency, ModelStats
from .pricing import PriceBook, compute_cost

_CENT = Decimal("0.01")


def detect_inefficiencies(
    stats: Sequence[ModelStats],
    book: PriceBook,
    as_of: date,
    *,
    window_days: int = 30,
    min_requests: int = 200,
    bloat_avg_tokens: int = 3000,
    bloat_ratio: float = 10.0,
    target_prompt_tokens: int = 1500,
    uncapped_avg_tokens: int = 1200,
    cap_saving_pct: float = 0.20,
    gap_ratio: float = 0.01,
    min_monthly_waste: Decimal = Decimal("5"),
) -> List[Inefficiency]:
    scale = Decimal(30) / Decimal(window_days)
    found: List[Inefficiency] = []
    for s in stats:
        price = book.lookup(s.model_name, as_of)

        # ---- telemetry gap: needs fewer requests, it is about missing data not volume
        total_calls = s.requests + s.malformed_count
        if s.malformed_count >= 10 and total_calls and s.malformed_count / total_calls >= gap_ratio:
            per_request = (
                compute_cost(s.prompt_tokens, s.completion_tokens, price) / s.requests
                if price is not None and s.requests else Decimal(0)
            )
            unattributed = (per_request * s.malformed_count * scale).quantize(_CENT)
            found.append(Inefficiency(
                s.department_id, s.model_name, "telemetry_gap", "warning", s.requests, unattributed,
                f"{s.malformed_count:,} of {total_calls:,} calls ({s.malformed_count / total_calls:.1%}) returned no usable "
                f"token counts, so their cost is not in the bill. Estimated unattributed spend shown.",
                "Check for streaming responses, error bodies or SDKs that omit `usage`; enable stream_options.include_usage.",
            ))

        if s.requests < min_requests or price is None:
            continue
        avg_prompt = s.prompt_tokens // s.requests
        avg_completion = s.completion_tokens // s.requests

        # ---- prompt bloat
        ratio = s.prompt_tokens / max(s.completion_tokens, 1)
        if avg_prompt >= bloat_avg_tokens and ratio >= bloat_ratio and avg_prompt > target_prompt_tokens:
            excess_tokens = (avg_prompt - target_prompt_tokens) * s.requests
            waste = (Decimal(excess_tokens) / 1000 * price.prompt_per_1k * scale).quantize(_CENT)
            if waste >= min_monthly_waste:
                found.append(Inefficiency(
                    s.department_id, s.model_name, "prompt_bloat",
                    "warning" if waste >= min_monthly_waste * 5 else "info", s.requests, waste,
                    f"Average prompt is {avg_prompt:,} tokens ({ratio:.0f}x the completion). Assuming prompts can be "
                    f"trimmed to ~{target_prompt_tokens:,} tokens.",
                    "Trim system prompts, cap retrieved chunks, summarise history, or use prompt caching.",
                ))

        # ---- uncapped completion
        if avg_completion >= uncapped_avg_tokens:
            cost = compute_cost(0, s.completion_tokens, price)
            waste = (cost * Decimal(str(cap_saving_pct)) * scale).quantize(_CENT)
            if waste >= min_monthly_waste:
                found.append(Inefficiency(
                    s.department_id, s.model_name, "uncapped_completion",
                    "warning" if waste >= min_monthly_waste * 5 else "info", s.requests, waste,
                    f"Average completion is {avg_completion:,} tokens. Assuming a max_tokens cap / concise-output "
                    f"instruction saves {cap_saving_pct:.0%} of output spend.",
                    "Set max_tokens per use case and ask for concise answers; stream long outputs only when needed.",
                ))
    return sorted(found, key=lambda i: i.est_monthly_waste, reverse=True)
