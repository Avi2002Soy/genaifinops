"""Assemble the JSON served by GET /api/report (and handy for ad-hoc checks)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict

from .attribution import build_showback
from .impact import summarize_impact


def build_report(repo, today: date, days: int = 30) -> Dict[str, Any]:
    since = today - timedelta(days=days)
    return {
        "window": {"from": since.isoformat(), "to": (today - timedelta(days=1)).isoformat(), "days": days},
        "showback": build_showback(repo.cost_lines(since)),
        "capture": repo.capture_summary(since),
        "impact": summarize_impact(repo.impact_results()),
        "open_opportunities_monthly": repo.open_opportunities(),
        "today_so_far": repo.live_totals(today),
    }
