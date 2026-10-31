"""Multi-model pricing catalog: validated JSON -> effective-dated price entries.

The catalog (database/pricing_catalog.json) is the single place to maintain per-model prices across
providers. `scripts/load_pricing.py` upserts it into dbo.ModelPricing. Validation catches the mistakes
that silently corrupt cost attribution: a fallback model that doesn't exist, that costs *more*, or that
belongs to another provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .models import PriceVersion
from .pricing import normalize_model


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    provider: str
    effective_from: date
    prompt_per_1k: Decimal
    completion_per_1k: Decimal
    fallback: Optional[str]

    def to_price_entry(self) -> Tuple[str, PriceVersion]:
        return self.name, PriceVersion(self.effective_from, self.prompt_per_1k, self.completion_per_1k, self.fallback)


def _dec(value: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise CatalogError(f"{what}: '{value}' is not a number") from None
    if d < 0:
        raise CatalogError(f"{what}: price cannot be negative")
    return d


def parse_catalog(data: Dict[str, Any]) -> List[CatalogEntry]:
    default_from = data.get("effective_from", "2000-01-01")
    entries: List[CatalogEntry] = []
    seen = set()
    for m in data.get("models", []):
        name = normalize_model(m.get("name"))
        if name == "unknown":
            raise CatalogError("a model entry has no name")
        try:
            eff = date.fromisoformat(m.get("effective_from", default_from))
        except ValueError:
            raise CatalogError(f"{name}: effective_from must be YYYY-MM-DD") from None
        if (name, eff) in seen:
            raise CatalogError(f"{name}: duplicate entry for {eff}")
        seen.add((name, eff))
        fb = normalize_model(m["fallback"]) if m.get("fallback") else None
        entries.append(CatalogEntry(
            name, str(m.get("provider", "unknown")), eff,
            _dec(m.get("prompt_per_1k"), f"{name}.prompt_per_1k"),
            _dec(m.get("completion_per_1k"), f"{name}.completion_per_1k"), fb,
        ))
    by_name = {e.name: e for e in entries}
    for e in entries:
        if not e.fallback:
            continue
        alt = by_name.get(e.fallback)
        if alt is None:
            raise CatalogError(f"{e.name}: fallback '{e.fallback}' is not in the catalog")
        if alt.name == e.name:
            raise CatalogError(f"{e.name}: cannot fall back to itself")
        if alt.provider != e.provider:
            raise CatalogError(f"{e.name}: fallback '{alt.name}' is from a different provider")
        if alt.prompt_per_1k + alt.completion_per_1k >= e.prompt_per_1k + e.completion_per_1k:
            raise CatalogError(f"{e.name}: fallback '{alt.name}' is not cheaper")
    return entries
