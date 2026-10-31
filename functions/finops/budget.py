"""Month-end projection and budget alert evaluation."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Mapping

from .models import Alert


@dataclass(frozen=True)
class Projection:
    month_to_date: Decimal
    projected_month_end: Decimal
    recent_daily_avg: Decimal
    days_remaining: int


def project_month_end(daily_costs: Mapping[date, Decimal], as_of: date) -> Projection:
    """Project the month's spend from actuals + the trailing-7-day run rate.

    `as_of` is the last *complete* day. The trailing window reacts to recent
    behaviour much faster than a naive month-to-date average.
    """
    month_start = as_of.replace(day=1)
    days_in_month = calendar.monthrange(as_of.year, as_of.month)[1]
    elapsed = as_of.day
    mtd = sum((daily_costs.get(month_start + timedelta(days=i), Decimal("0")) for i in range(elapsed)), Decimal("0"))
    n = min(7, elapsed)
    recent = sum((daily_costs.get(as_of - timedelta(days=i), Decimal("0")) for i in range(n)), Decimal("0")) / n
    remaining = days_in_month - elapsed
    return Projection(mtd, mtd + recent * remaining, recent, remaining)


def evaluate_budgets(
    budgets: Mapping[str, Decimal],
    daily_costs_by_dept: Mapping[str, Mapping[date, Decimal]],
    as_of: date,
    *,
    warn_pct: float = 0.80,
    min_days_for_forecast: int = 3,
) -> List[Alert]:
    alerts: List[Alert] = []
    month = as_of.strftime("%Y-%m")
    for dept, budget in budgets.items():
        if budget <= 0:
            continue
        proj = project_month_end(daily_costs_by_dept.get(dept, {}), as_of)
        payload: Dict[str, object] = {
            "budget": str(budget),
            "month_to_date": str(proj.month_to_date.quantize(Decimal("0.01"))),
            "projected": str(proj.projected_month_end.quantize(Decimal("0.01"))),
            "month": month,
        }
        mtd, projected = proj.month_to_date, proj.projected_month_end
        if mtd >= budget:
            alerts.append(Alert(
                f"budget_critical:{dept}:{month}", "budget_critical", "critical",
                f"{dept} has spent ${mtd:.2f} of its ${budget:.2f} budget for {month}.", dept, payload))
            continue
        if mtd >= budget * Decimal(str(warn_pct)):
            alerts.append(Alert(
                f"budget_warning:{dept}:{month}", "budget_warning", "warning",
                f"{dept} has used {mtd / budget:.0%} of its ${budget:.2f} budget for {month}.", dept, payload))
        if as_of.day >= min_days_for_forecast and projected > budget:
            alerts.append(Alert(
                f"budget_forecast:{dept}:{month}", "budget_forecast", "warning",
                f"{dept} is projected to reach ${projected:.2f} against a ${budget:.2f} budget for {month}.",
                dept, payload))
    return alerts
