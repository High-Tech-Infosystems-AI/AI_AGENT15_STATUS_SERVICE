"""Inbound provider webhooks: store the raw event, then map what we understand.

# UNCONFIRMED: the vendor has not documented the webhook payload (plan §4.4
# #3/#4). The provider adapter maps Meta Cloud API-style payloads; matching a
# status to our log is best-effort, in this order:
#   1. provider_message_id (wamid)
#   2. campaignName (= our correlation_id "cbk_<log_id>") / batchId (= provider_ref)
#   3. recipient phone + the most recent submitted/sent log within 24 h
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.whatsapp_layer import sender
from app.whatsapp_layer.models import (
    WhatsAppMessageLog as L, WhatsAppProviderAccount as A, WhatsAppTemplate as T,
    WhatsAppWebhookEvent,
)
from app.whatsapp_layer.providers.base import WebhookEvent

logger = logging.getLogger("app_logger")

RETENTION_DAYS = 30
_DROP_HEADERS = {"authorization", "cookie", "x-internal-token"}


def find_account(db: Session, provider: str, webhook_token: str) -> Optional[A]:
    if not webhook_token:
        return None
    return (db.query(A).filter(A.webhook_token == webhook_token, A.provider == provider)
            .first())


def store_event(db: Session, account: A, headers: Dict[str, str], raw: bytes) -> WhatsAppWebhookEvent:
    text = raw.decode("utf-8", errors="replace") if raw else ""
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    ev = WhatsAppWebhookEvent(
        account_id=account.id, organization_id=account.organization_id,
        received_at=datetime.utcnow(),
        headers={k: v for k, v in (headers or {}).items() if k.lower() not in _DROP_HEADERS},
        body=body if isinstance(body, (dict, list)) else None,
        body_raw=None if isinstance(body, (dict, list)) else text[:100000],
        processed=0,
    )
    db.add(ev)
    db.commit()
    return ev


def _match_log(db: Session, account: A, ev: WebhookEvent, at: datetime) -> Optional[L]:
    base = db.query(L).filter(L.organization_id == account.organization_id)
    # UNCONFIRMED: matching keys depend on the (undocumented) payload.
    if ev.provider_message_id:
        log = base.filter(L.provider_message_id == ev.provider_message_id).first()
        if log:
            return log
    if ev.campaign_name:
        log = base.filter(L.correlation_id == ev.campaign_name).first()
        if log:
            return log
    if ev.batch_id:
        log = base.filter(L.provider_ref == str(ev.batch_id)).first()
        if log:
            return log
    if ev.recipient:
        phone = "+" + "".join(ch for ch in ev.recipient if ch.isdigit())
        log = (base.filter(L.phone_e164 == phone,
                           L.status.in_(("submitted", "sent", "delivered")),
                           L.submitted_at >= at - timedelta(hours=24))
               .order_by(L.submitted_at.desc(), L.id.desc()).first())
        if log:
            # Remember the wamid so later events match directly.
            if ev.provider_message_id and not log.provider_message_id:
                log.provider_message_id = ev.provider_message_id
            return log
    return None


def apply_events(db: Session, account: A, events: List[WebhookEvent]) -> List[int]:
    """Apply parsed events; returns the ids of logs that changed."""
    from app.whatsapp_layer import alerts
    changed: List[int] = []
    now = datetime.utcnow()
    for ev in events:
        if ev.kind == "message_status":
            at = datetime.utcfromtimestamp(ev.timestamp) if ev.timestamp else now
            log = _match_log(db, account, ev, at)
            if log is None or not ev.status:
                continue
            if ev.status == "failed":
                ok = sender.advance_status(log, "failed", at, ev.error_code, ev.error_message)
            else:
                ok = sender.advance_status(log, ev.status, at)
            if ok:
                changed.append(log.id)
        elif ev.kind == "template_status" and ev.status:
            q = db.query(T).filter(T.organization_id == account.organization_id,
                                   T.deleted_at.is_(None))
            tpl = None
            if ev.provider_template_id:
                tpl = q.filter(T.provider_template_id == ev.provider_template_id).first()
            if tpl is None and ev.template_name:
                tq = q.filter(T.name == ev.template_name)
                if ev.template_language:
                    tq = tq.filter(T.language == ev.template_language)
                tpl = tq.first()
            if tpl is None or tpl.approval_status == ev.status:
                continue
            tpl.approval_status = ev.status
            if ev.status == "rejected":
                tpl.rejection_reason = ev.reason or tpl.rejection_reason
            tpl.last_synced_at = now
            logger.info("Webhook: template %s (%s) → %s", tpl.id, tpl.name, ev.status)
            if ev.status == "rejected":
                db.commit()
                alerts.notify_org_admins(
                    db, account.organization_id,
                    title="WhatsApp template rejected",
                    message=f"Template '{tpl.name}' was rejected: {tpl.rejection_reason or 'no reason given'}.",
                    dedupe_key=f"template_rejected:{tpl.id}", event="alert_template_rejected")
    db.commit()
    return changed


def process(db: Session, account: A, headers: Dict[str, str], raw: bytes) -> None:
    """Store first (never lose an event), then parse + apply; errors are kept
    on the event row. Never raises."""
    from app.whatsapp_layer.providers.registry import get_provider
    ev_row = store_event(db, account, headers, raw)
    try:
        # Non-JSON bodies are kept raw on the row and yield no events.
        events = get_provider(account.provider).parse_webhook(headers, ev_row.body)
        changed = apply_events(db, account, events)
        ev_row.processed = 1
        ev_row.matched_log_ids = changed or None
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Webhook event %s processing failed: %s", ev_row.id, exc, exc_info=True)
        try:
            ev_row = db.query(WhatsAppWebhookEvent).filter(WhatsAppWebhookEvent.id == ev_row.id).first()
            if ev_row:
                ev_row.error = str(exc)[:2000]
                db.commit()
        except Exception:
            db.rollback()


def cleanup(db: Session, now: Optional[datetime] = None) -> int:
    now = now or datetime.utcnow()
    n = (db.query(WhatsAppWebhookEvent)
         .filter(WhatsAppWebhookEvent.received_at < now - timedelta(days=RETENTION_DAYS))
         .delete(synchronize_session=False))
    db.commit()
    return n
