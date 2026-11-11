"""WhatsApp Flow alerts (webhook field "flows", CONFIRMED on staging):

  {"event": "ENDPOINT_AVAILABILITY", "flow_id": "...", "message": "Flow endpoint
   availability was below 90% threshold in the last 60 minutes. ...",
   "threshold": 90, "alert_state": "ACTIVATED", "availability": 0}

Meta repeats an ACTIVATED alert while the problem lasts, so admins are told
once per flow + event + state (Redis, 24 h); a DEACTIVATED / RESOLVED alert
clears it and, when we had alerted, says the endpoint recovered. Meta's
FLOW_STATUS_CHANGE (old_status → new_status) notifies on BLOCKED /
THROTTLED / DEPRECATED. The flow itself lives in the Campaign Service, which
gets the same event forwarded (campaign_forward) and stores the alert state
on the flow for the Flows tab.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.whatsapp_layer.providers.base import WebhookEvent

logger = logging.getLogger("app_logger")

DEDUPE_TTL = 24 * 3600
ACTIVE_STATES = ("ACTIVATED", "ACTIVE", "FIRING")
CLEAR_STATES = ("DEACTIVATED", "RESOLVED", "INACTIVE")
BAD_FLOW_STATUSES = ("BLOCKED", "THROTTLED", "DEPRECATED")

_EVENT_LABEL = {
    "ENDPOINT_AVAILABILITY": "endpoint unavailable",
    "ENDPOINT_ERROR_RATE": "endpoint errors",
    "ENDPOINT_LATENCY": "endpoint slow",
    "CLIENT_ERROR_RATE": "client errors",
}


def _key(flow_id: str, event: str, state: str) -> str:
    return f"flow:{flow_id}:{event}:{state}"


def flows_link(account) -> str:
    return f"/settings/channels/whatsapp/{account.id}?tab=flows"


def handle(db: Session, account, ev: WebhookEvent) -> Optional[int]:
    """Notify org admins about one Flow event. Returns the notification id."""
    from app.whatsapp_layer import alerts
    from app.whatsapp_layer import events as EV
    if not ev.flow_id or not ev.event:
        return None
    org = account.organization_id
    raw = ev.raw if isinstance(ev.raw, dict) else {}
    meta = {"flow_id": ev.flow_id, "flow_event": ev.event, "alert_state": ev.alert_state,
            "account_id": account.id, "kind": "flow_alert", "link": flows_link(account)}
    label = _EVENT_LABEL.get(ev.event, ev.event.replace("_", " ").lower())
    state = (ev.alert_state or "").upper()

    if ev.event == "FLOW_STATUS_CHANGE":
        new = str(raw.get("new_status") or "").upper()
        if new not in BAD_FLOW_STATUSES or not alerts.claim(org, _key(ev.flow_id, ev.event, new),
                                                             DEDUPE_TTL):
            return None
        return alerts.notify_org_admins(
            db, org, title=f"WhatsApp Flow {new.lower()}",
            message=f"Meta changed WhatsApp Flow {ev.flow_id} to {new}"
                    f"{' (was ' + str(raw.get('old_status')) + ')' if raw.get('old_status') else ''}. "
                    f"Campaigns using it may not open for recipients.",
            metadata=meta, event=EV.ALERT_FLOW_ENDPOINT)

    if state in CLEAR_STATES:
        had_alert = alerts.release(org, [_key(ev.flow_id, ev.event, s) for s in ACTIVE_STATES])
        if not had_alert:
            return None
        return alerts.notify_org_admins(
            db, org, title=f"WhatsApp Flow {label} — resolved",
            message=f"WhatsApp Flow {ev.flow_id}: {ev.reason or 'the alert has cleared'}.",
            metadata=meta, event=EV.ALERT_FLOW_ENDPOINT)

    if state and state not in ACTIVE_STATES:
        logger.info("Flow %s event %s with unknown alert_state %s", ev.flow_id, ev.event, state)
        return None
    if not alerts.claim(org, _key(ev.flow_id, ev.event, state or "ACTIVATED"), DEDUPE_TTL):
        return None
    return alerts.notify_org_admins(
        db, org, title=f"WhatsApp Flow {label}",
        message=(f"WhatsApp Flow {ev.flow_id}: {ev.reason or label}. Recipients may not be "
                 f"able to use the flow until its endpoint is fixed — check the Flows tab."),
        metadata=meta, event=EV.ALERT_FLOW_ENDPOINT)
