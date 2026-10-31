"""Cost-spike detection using a robust (median/MAD) z-score.

Median/MAD instead of mean/stdev so that one earlier spike does not inflate the
baseline and hide the next one. Pure Python: no ML service, no extra cost.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Mapping, Optional

MAX_SCORE = 99.99


@dataclass(frozen=True)
class Anomaly:
    department_id: str
    usage_date: date
    observed: Decimal
    expected: Decimal
    score: float
    ratio: Optional[float]  # observed / expected (None when expected == 0)


def robust_score(value: float, history: List[float]) -> float:
    """How many 'robust sigmas' `value` sits above the median of `history`.

    sigma = 1.4826 * MAD (equivalent to the classic 0.6745 * (x - median) / MAD).
    When more than half the history is identical MAD is 0; falling back to the mean absolute
    deviation would let one old spike inflate the scale and hide the next one, so the scale
    is floored at 10% of the median instead (and any rise above a zero baseline is maximal).
    """
    med = statistics.median(history)
    mad = statistics.median(abs(x - med) for x in history)
    sigma = 1.4826 * mad
    if sigma <= 0:
        sigma = 0.1 * abs(med)
    if sigma <= 0:
        return MAX_SCORE if value > med else 0.0
    return (value - med) / sigma


def detect_anomalies(
    costs_by_dept: Mapping[str, Mapping[date, Decimal]],
    target_date: date,
    *,
    window_days: int = 14,
    min_history_days: int = 7,
    threshold: float = 3.5,
    min_abs_delta: Decimal = Decimal("1.00"),
) -> List[Anomaly]:
    found: List[Anomaly] = []
    window_start = target_date - timedelta(days=window_days)
    for dept, series in costs_by_dept.items():
        observed_days = [d for d in series if d <= target_date]
        if not observed_days or target_date not in series:
            continue
        # Don't treat days before the department started using the gateway as zero-cost days.
        first_seen = max(min(observed_days), window_start)
        history: List[float] = []
        d = first_seen
        while d < target_date:
            history.append(float(series.get(d, Decimal("0"))))
            d += timedelta(days=1)
        if len(history) < min_history_days:
            continue
        value = series[target_date]
        score = min(robust_score(float(value), history), MAX_SCORE)
        expected = Decimal(str(round(statistics.median(history), 6)))
        if score >= threshold and (value - expected) >= min_abs_delta:
            ratio = float(value / expected) if expected > 0 else None
            found.append(Anomaly(dept, target_date, value, expected, round(score, 2), ratio))
    return sorted(found, key=lambda a: a.observed - a.expected, reverse=True)
