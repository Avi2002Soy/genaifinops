"""Cost attribution ("showback"): who spent what, on which model, at what unit cost."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Sequence

from .models import CostLine

_CENT = Decimal("0.01")
_SIX_DP = Decimal("0.000001")


def _pct(part: Decimal, whole: Decimal) -> float:
    return 0.0 if whole <= 0 else round(float(part / whole * 100), 1)


def build_showback(lines: Sequence[CostLine]) -> Dict[str, Any]:
    total = sum((l.cost for l in lines), Decimal("0"))
    total_requests = sum(l.requests for l in lines)

    by_dept: Dict[str, List[CostLine]] = defaultdict(list)
    by_model: Dict[str, List[CostLine]] = defaultdict(list)
    for l in lines:
        by_dept[l.department_id].append(l)
        by_model[l.model_name].append(l)

    departments = []
    for dept, items in by_dept.items():
        cost = sum((i.cost for i in items), Decimal("0"))
        requests = sum(i.requests for i in items)
        top = max(items, key=lambda i: i.cost)
        departments.append({
            "department_id": dept,
            "cost": cost.quantize(_CENT),
            "share_pct": _pct(cost, total),
            "requests": requests,
            "cost_per_request": (cost / requests).quantize(_SIX_DP) if requests else None,
            "top_model": top.model_name,
            "top_model_share_pct": _pct(top.cost, cost),
        })
    models = []
    for model, items in by_model.items():
        cost = sum((i.cost for i in items), Decimal("0"))
        requests = sum(i.requests for i in items)
        tokens = sum(i.total_tokens for i in items)
        models.append({
            "model_name": model,
            "cost": cost.quantize(_CENT),
            "share_pct": _pct(cost, total),
            "requests": requests,
            "cost_per_1k_tokens": (cost / (Decimal(tokens) / 1000)).quantize(_SIX_DP) if tokens else None,
        })
    return {
        "total_cost": total.quantize(_CENT),
        "total_requests": total_requests,
        "departments": sorted(departments, key=lambda d: d["cost"], reverse=True),
        "models": sorted(models, key=lambda m: m["cost"], reverse=True),
    }
