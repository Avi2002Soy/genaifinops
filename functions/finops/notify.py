"""Alert delivery. Webhook-only on purpose: stdlib, no extra dependency, free."""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional, Sequence

from .models import Alert

_ICON = {"critical": "\U0001F6A8", "warning": "\u26A0\uFE0F", "info": "\u2139\uFE0F"}


def format_alerts(alerts: Sequence[Alert]) -> str:
    lines = [f"*GenAI FinOps: {len(alerts)} new alert(s)*"]
    lines += [f"{_ICON.get(a.severity, '')} [{a.type}] {a.message}" for a in alerts]
    return "\n".join(lines)


class WebhookNotifier:
    """POSTs {"text": ...}: accepted by Slack incoming webhooks and Teams Workflows webhooks
    configured to read a `text` field. Failures are logged, never raised: alerting must not
    break ingestion."""

    def __init__(self, url: Optional[str]):
        self._url = url

    def send(self, alerts: Sequence[Alert]) -> bool:
        if not alerts:
            return False
        text = format_alerts(alerts)
        if not self._url:
            logging.warning("No ALERT_WEBHOOK_URL configured; alerts logged only:\n%s", text)
            return False
        req = urllib.request.Request(
            self._url,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - URL comes from app settings
                return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001
            logging.exception("Alert webhook delivery failed")
            return False
