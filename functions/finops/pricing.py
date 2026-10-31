"""Model-name normalisation, effective-dated price book and cost calculation."""
from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import PriceVersion, PricedUsage, UsageRow

_SIX_DP = Decimal("0.000001")
# gpt-4-0613, gpt-4o-2024-08-06, gpt-35-turbo-20230613 ...
_VERSION_SUFFIX = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8}|\d{4})$")
_ALIASES = {
    "gpt-35-turbo": "gpt-3.5-turbo",
    "gpt-35-turbo-16k": "gpt-3.5-turbo-16k",
}


def normalize_model(name: Optional[str]) -> str:
    """Collapse deployment/version variants so one model maps to one price row."""
    n = (name or "").strip().lower().replace("_", "-")
    if not n:
        return "unknown"
    n = _VERSION_SUFFIX.sub("", n)
    return _ALIASES.get(n, n)


class PriceBook:
    """Effective-dated prices: the price in force on a given day is used.

    Re-pricing history is therefore correct after a vendor price change, which
    a single-row-per-model table cannot guarantee.
    """

    def __init__(self, entries: Iterable[Tuple[str, PriceVersion]]):
        grouped: Dict[str, List[PriceVersion]] = defaultdict(list)
        for model, version in entries:
            grouped[normalize_model(model)].append(version)
        self._versions: Dict[str, List[PriceVersion]] = {}
        self._dates: Dict[str, List[date]] = {}
        for model, versions in grouped.items():
            versions.sort(key=lambda v: v.effective_from)
            self._versions[model] = versions
            self._dates[model] = [v.effective_from for v in versions]

    def lookup(self, model: str, on: date) -> Optional[PriceVersion]:
        key = normalize_model(model)
        dates = self._dates.get(key)
        if not dates:
            return None
        idx = bisect_right(dates, on) - 1
        return self._versions[key][idx] if idx >= 0 else None

    def models(self) -> List[str]:
        return sorted(self._versions)


def compute_cost(prompt_tokens: int, completion_tokens: int, price: PriceVersion) -> Decimal:
    cost = (Decimal(prompt_tokens) / 1000) * price.prompt_per_1k + (
        Decimal(completion_tokens) / 1000
    ) * price.completion_per_1k
    return cost.quantize(_SIX_DP, rounding=ROUND_HALF_UP)


def consolidate(rows: Iterable[UsageRow]) -> List[UsageRow]:
    """Normalise model names, then merge rows that now share a primary key.

    Without this, `gpt-4-0613` and `gpt-4` rows for the same day/department
    would collide on the (UsageDate, DepartmentId, ModelName) key.
    """
    merged: Dict[Tuple[date, str, str], UsageRow] = {}
    for r in rows:
        key = (r.usage_date, r.department_id, normalize_model(r.model_name))
        cur = merged.get(key)
        if cur is None:
            merged[key] = UsageRow(**{**r.__dict__, "model_name": key[2]})
            continue

        def _max(a: Optional[int], b: Optional[int]) -> Optional[int]:
            vals = [v for v in (a, b) if v is not None]
            return max(vals) if vals else None

        total_req = cur.request_count + r.request_count
        if cur.avg_latency_ms is not None and r.avg_latency_ms is not None and total_req:
            avg_latency: Optional[int] = round(
                (cur.avg_latency_ms * cur.request_count + r.avg_latency_ms * r.request_count)
                / total_req
            )
        else:
            avg_latency = cur.avg_latency_ms if cur.avg_latency_ms is not None else r.avg_latency_ms
        merged[key] = UsageRow(
            usage_date=cur.usage_date,
            department_id=cur.department_id,
            model_name=key[2],
            prompt_tokens=cur.prompt_tokens + r.prompt_tokens,
            completion_tokens=cur.completion_tokens + r.completion_tokens,
            request_count=total_req,
            malformed_count=cur.malformed_count + r.malformed_count,
            p95_total_tokens=_max(cur.p95_total_tokens, r.p95_total_tokens),
            avg_latency_ms=avg_latency,
            p95_latency_ms=_max(cur.p95_latency_ms, r.p95_latency_ms),
        )
    return sorted(merged.values(), key=lambda r: (r.usage_date, r.department_id, r.model_name))


def price_usage(rows: Sequence[UsageRow], book: PriceBook) -> List[PricedUsage]:
    priced: List[PricedUsage] = []
    for row in rows:
        version = book.lookup(row.model_name, row.usage_date)
        if version is None:
            priced.append(PricedUsage(row, Decimal("0"), False))
        else:
            cost = compute_cost(row.prompt_tokens, row.completion_tokens, version)
            priced.append(PricedUsage(row, cost, True))
    return priced
