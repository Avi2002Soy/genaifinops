"""Runtime configuration, read once from environment variables (Function App settings)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Optional


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_decimal(name: str, default: str) -> Decimal:
    return Decimal(os.environ.get(name) or default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    workspace_id: str
    sql_connection_string: str

    # --- SQL access -------------------------------------------------------
    # True  -> authenticate with the Function's managed identity (no password).
    #          The connection string must then NOT contain UID/PWD/Authentication.
    sql_use_managed_identity: bool = False
    # Serverless free-tier DBs auto-pause; the first login after a pause fails
    # (error 40613) until the DB resumes (~30-60 s). We retry with backoff.
    sql_max_attempts: int = 5
    sql_retry_base_seconds: float = 10.0

    # --- ingestion --------------------------------------------------------
    # Re-aggregate the last N complete UTC days on every run. Idempotent upserts
    # make this safe, and it absorbs late-arriving telemetry.
    lookback_days: int = 2

    # --- analytics --------------------------------------------------------
    history_days: int = 35
    anomaly_window_days: int = 14
    anomaly_min_history_days: int = 7
    anomaly_score_threshold: float = 3.5
    anomaly_min_abs_delta_usd: Decimal = Decimal("1.00")
    budget_warn_pct: float = 0.80
    capture_rate_floor: float = 0.999
    capture_min_events: int = 1000

    # --- model routing ----------------------------------------------------
    routing_window_days: int = 30
    routing_min_requests: int = 200
    routing_max_avg_prompt_tokens: int = 800
    routing_max_p95_total_tokens: int = 2000
    routing_min_monthly_savings_usd: Decimal = Decimal("5")

    # --- real-time ingest (APIM -> Event Hub / HTTP) ----------------------
    ingest_max_age_days: int = 7  # events older than this are dead-lettered (stale clock / replay)
    ingest_max_future_minutes: int = 10
    ingest_max_body_bytes: int = 1_000_000
    ingest_retention_days: int = 14  # hourly rows, batch ids and dead letters older than this are purged

    # --- capture-reliability reconciliation ---------------------------------
    # Resource id of the APIM instance, e.g.
    # /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ApiManagement/service/<name>
    apim_resource_id: Optional[str] = None
    # Which gateway requests *should* have produced a telemetry event. The policy only emits for
    # HTTP 200 JSON responses, so the default filters the denominator the same way.
    apim_metric_filter: str = "GatewayResponseCode eq '200'"
    reconcile_lookback_days: int = 2
    reconcile_min_gateway_requests: int = 1000

    # --- inefficiency detection ---------------------------------------------
    eff_window_days: int = 30
    eff_min_requests: int = 200
    eff_prompt_bloat_avg_tokens: int = 3000
    eff_prompt_bloat_ratio: float = 10.0  # prompt:completion tokens
    eff_prompt_target_tokens: int = 1500  # what a trimmed prompt is assumed to look like
    eff_uncapped_completion_avg_tokens: int = 1200
    eff_completion_cap_saving_pct: float = 0.20
    eff_telemetry_gap_ratio: float = 0.01
    eff_min_monthly_waste_usd: Decimal = Decimal("5")

    # --- routing impact (did accepting a recommendation actually help?) -----
    impact_window_days: int = 14
    impact_min_days_after: int = 7
    impact_min_requests: int = 200

    # --- notifications ----------------------------------------------------
    alert_webhook_url: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            workspace_id=os.environ["LOG_ANALYTICS_WORKSPACE_ID"],
            sql_connection_string=os.environ["SQL_CONNECTION_STRING"],
            sql_use_managed_identity=_env_bool("SQL_USE_MANAGED_IDENTITY"),
            sql_max_attempts=_env_int("SQL_MAX_ATTEMPTS", 5),
            sql_retry_base_seconds=_env_float("SQL_RETRY_BASE_SECONDS", 10.0),
            lookback_days=_env_int("LOOKBACK_DAYS", 2),
            history_days=_env_int("HISTORY_DAYS", 35),
            anomaly_window_days=_env_int("ANOMALY_WINDOW_DAYS", 14),
            anomaly_min_history_days=_env_int("ANOMALY_MIN_HISTORY_DAYS", 7),
            anomaly_score_threshold=_env_float("ANOMALY_SCORE_THRESHOLD", 3.5),
            anomaly_min_abs_delta_usd=_env_decimal("ANOMALY_MIN_ABS_DELTA_USD", "1.00"),
            budget_warn_pct=_env_float("BUDGET_WARN_PCT", 0.80),
            capture_rate_floor=_env_float("CAPTURE_RATE_FLOOR", 0.999),
            capture_min_events=_env_int("CAPTURE_MIN_EVENTS", 1000),
            routing_window_days=_env_int("ROUTING_WINDOW_DAYS", 30),
            routing_min_requests=_env_int("ROUTING_MIN_REQUESTS", 200),
            routing_max_avg_prompt_tokens=_env_int("ROUTING_MAX_AVG_PROMPT_TOKENS", 800),
            routing_max_p95_total_tokens=_env_int("ROUTING_MAX_P95_TOTAL_TOKENS", 2000),
            routing_min_monthly_savings_usd=_env_decimal("ROUTING_MIN_MONTHLY_SAVINGS_USD", "5"),
            ingest_max_age_days=_env_int("INGEST_MAX_AGE_DAYS", 7),
            ingest_max_future_minutes=_env_int("INGEST_MAX_FUTURE_MINUTES", 10),
            ingest_max_body_bytes=_env_int("INGEST_MAX_BODY_BYTES", 1_000_000),
            ingest_retention_days=_env_int("INGEST_RETENTION_DAYS", 14),
            apim_resource_id=os.environ.get("APIM_RESOURCE_ID") or None,
            apim_metric_filter=os.environ.get("APIM_METRIC_FILTER") or "GatewayResponseCode eq '200'",
            reconcile_lookback_days=_env_int("RECONCILE_LOOKBACK_DAYS", 2),
            reconcile_min_gateway_requests=_env_int("RECONCILE_MIN_GATEWAY_REQUESTS", 1000),
            eff_window_days=_env_int("EFF_WINDOW_DAYS", 30),
            eff_min_requests=_env_int("EFF_MIN_REQUESTS", 200),
            eff_prompt_bloat_avg_tokens=_env_int("EFF_PROMPT_BLOAT_AVG_TOKENS", 3000),
            eff_prompt_bloat_ratio=_env_float("EFF_PROMPT_BLOAT_RATIO", 10.0),
            eff_prompt_target_tokens=_env_int("EFF_PROMPT_TARGET_TOKENS", 1500),
            eff_uncapped_completion_avg_tokens=_env_int("EFF_UNCAPPED_COMPLETION_AVG_TOKENS", 1200),
            eff_completion_cap_saving_pct=_env_float("EFF_COMPLETION_CAP_SAVING_PCT", 0.20),
            eff_telemetry_gap_ratio=_env_float("EFF_TELEMETRY_GAP_RATIO", 0.01),
            eff_min_monthly_waste_usd=_env_decimal("EFF_MIN_MONTHLY_WASTE_USD", "5"),
            impact_window_days=_env_int("IMPACT_WINDOW_DAYS", 14),
            impact_min_days_after=_env_int("IMPACT_MIN_DAYS_AFTER", 7),
            impact_min_requests=_env_int("IMPACT_MIN_REQUESTS", 200),
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL") or None,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
