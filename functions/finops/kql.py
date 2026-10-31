"""Log Analytics source: runs one aggregate KQL query for a window of complete UTC days."""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .models import UsageRow

# Workspace-based Application Insights exposes telemetry in the AppTraces table
# (columns Message / Properties / TimeGenerated). The classic `traces` /
# `customDimensions` names only work when querying the App Insights resource itself.
# Keep in sync with queries/token_analysis.kql.
USAGE_QUERY = """
AppTraces
| where Message == "LLM Token Telemetry"
| extend DepartmentId = tostring(Properties.DepartmentId),
         ModelName    = tolower(tostring(Properties.ModelName)),
         Prompt       = tolong(Properties.PromptTokens),
         Completion   = tolong(Properties.CompletionTokens),
         LatencyMs    = tolong(Properties.LatencyMs)
| extend DepartmentId = iff(isempty(DepartmentId), "Unknown", DepartmentId),
         ModelName    = iff(isempty(ModelName), "unknown", ModelName),
         Valid        = isnotnull(Prompt) and isnotnull(Completion) and Prompt >= 0 and Completion >= 0
| extend HasLatency   = Valid and isnotnull(LatencyMs) and LatencyMs >= 0
| summarize
    TotalPromptTokens     = sumif(Prompt, Valid),
    TotalCompletionTokens = sumif(Completion, Valid),
    RequestCount          = countif(Valid),
    MalformedCount        = countif(not(Valid)),
    P95TotalTokens        = percentile(iff(Valid, Prompt + Completion, long(null)), 95),
    AvgLatencyMs          = avgif(LatencyMs, HasLatency),
    P95LatencyMs          = percentile(iff(HasLatency, LatencyMs, long(null)), 95)
  by DepartmentId, ModelName, UsageDate = startofday(TimeGenerated)
"""


class PartialQueryError(RuntimeError):
    """Log Analytics returned partial data; writing it would under-count usage."""


def _int(value: Any, default: Optional[int] = 0) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    return int(round(float(value)))


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def rows_from_table(columns: List[Any], rows: List[Any]) -> List[UsageRow]:
    names = [getattr(c, "name", c) for c in columns]
    out: List[UsageRow] = []
    for raw in rows:
        r: Dict[str, Any] = dict(zip(names, raw))
        out.append(UsageRow(
            usage_date=_to_date(r["UsageDate"]),
            department_id=str(r["DepartmentId"])[:100],
            model_name=str(r["ModelName"])[:100],
            prompt_tokens=_int(r.get("TotalPromptTokens")),
            completion_tokens=_int(r.get("TotalCompletionTokens")),
            request_count=_int(r.get("RequestCount")),
            malformed_count=_int(r.get("MalformedCount")),
            p95_total_tokens=_int(r.get("P95TotalTokens"), None),
            avg_latency_ms=_int(r.get("AvgLatencyMs"), None),
            p95_latency_ms=_int(r.get("P95LatencyMs"), None),
        ))
    return out


class LogAnalyticsSource:
    def __init__(self, workspace_id: str):
        # Imported lazily so the pure-Python modules stay importable (and testable) without Azure SDKs.
        from azure.identity import DefaultAzureCredential
        from azure.monitor.query import LogsQueryClient

        self._workspace_id = workspace_id
        self._client = LogsQueryClient(DefaultAzureCredential())

    def fetch(self, start: datetime, end: datetime) -> List[UsageRow]:
        from azure.monitor.query import LogsQueryStatus

        response = self._client.query_workspace(self._workspace_id, USAGE_QUERY, timespan=(start, end))
        if response.status == LogsQueryStatus.PARTIAL:
            raise PartialQueryError(f"Partial result for {start:%F}..{end:%F}: {response.partial_error}")
        rows: List[UsageRow] = []
        for table in response.tables:
            rows.extend(rows_from_table(list(table.columns), list(table.rows)))
        logging.info("Log Analytics returned %d aggregate rows for %s..%s", len(rows), start.date(), end.date())
        return rows
