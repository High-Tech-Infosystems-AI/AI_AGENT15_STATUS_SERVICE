"""Forward campaign WhatsApp events to the Campaign Service.

An Omnichannel account has one webhook URL, and it stays here. Events the
Campaign Service needs are forwarded, unchanged, to
POST {CAMPAIGN_SERVICE_URL}/campaigns/internal/events with X-Internal-Token
and X-Org-Id:
  - message statuses whose campaignName starts with "cmp" (campaign sends);
  - inbound messages (STOP / YES replies, Flow answers): the Campaign Service
    matches them to its own recipients and ignores the rest;
  - template status updates (campaign templates live in its own table).

Forwarding runs on a daemon thread with a short timeout, so the webhook
still answers the provider quickly; a failed forward is only logged.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List

import requests

from app.whatsapp_layer.providers.base import WebhookEvent

logger = logging.getLogger("app_logger")

TIMEOUT_SECONDS = 5
_FORWARD_HEADERS = {"content-type", "user-agent", "x-hub-signature-256"}


def is_campaign_relevant(events: List[WebhookEvent]) -> bool:
    for ev in events:
        if ev.kind in ("inbound", "template_status"):
            return True
        if ev.kind == "message_status" and str(ev.campaign_name or "").startswith("cmp"):
            return True
    return False


def _post(url: str, token: str, org_id: int, headers: Dict[str, str], raw: bytes) -> None:
    out = {k: v for k, v in (headers or {}).items() if k.lower() in _FORWARD_HEADERS}
    out.update({"X-Internal-Token": token, "X-Org-Id": str(org_id)})
    out.setdefault("content-type", "application/json")
    try:
        resp = requests.post(url, data=raw, headers=out, timeout=TIMEOUT_SECONDS)
        if resp.status_code >= 400:
            logger.warning("Campaign event forward returned HTTP %s", resp.status_code)
    except requests.RequestException as exc:
        logger.warning("Campaign event forward failed: %s", exc)


def forward(organization_id: int, headers: Dict[str, str], raw: bytes,
            events: List[WebhookEvent], *, background: bool = True) -> bool:
    """Forward when relevant and configured. Returns True when a forward was started."""
    from app.core import settings
    base = (getattr(settings, "CAMPAIGN_SERVICE_URL", "") or "").strip().rstrip("/")
    token = getattr(settings, "INTERNAL_SERVICE_TOKEN", "") or ""
    if not base or not token or not raw or not is_campaign_relevant(events):
        return False
    url = f"{base}/campaigns/internal/events"
    if background:
        threading.Thread(target=_post, args=(url, token, organization_id, headers, raw),
                         daemon=True).start()
    else:
        _post(url, token, organization_id, headers, raw)
    return True
