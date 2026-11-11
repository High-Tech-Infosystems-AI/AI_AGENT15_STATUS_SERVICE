"""Inbound provider webhooks: store the raw event, then map what we understand.

Omnichannel forwards Meta's WhatsApp Business webhook body unchanged
(CONFIRMED on staging for message statuses, template status updates and
Flow alerts; see OmnichannelProvider.parse_webhook). Template status /
quality / category changes go through template_status.apply_change (the
same path as the polling sync); Flow alerts through flow_alerts.handle.

Matching a message status to our log, in this order:
  1. provider_message_id (wamid)
  2. campaignName (= our correlation_id "cbk_<log_id>") / batchId (= provider_ref,
     also read from biz_opaque_callback_data.batch_id)
  3. recipient phone + the most recent submitted/sent log within 24 h
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.whatsapp_layer import sender
from app.whatsapp_layer.models import (
    WhatsAppMessageLog as L, WhatsAppProviderAccount as A, WhatsAppWebhookEvent,
)
from app.whatsapp_layer.providers.base import WebhookEvent

logger = logging.getLogger("app_logger")

RETENTION_DAYS = 30
_DROP_HEADERS = {"authorization", "cookie", "x-internal-token"}


def find_account(db: Session, provider: str, webhook_token: str) -> Optional[A]:
    if not webhook_token:
        return None
    # A deleted account's token no longer identifies anything.
    return (db.query(A).filter(A.webhook_token == webhook_token, A.provider == provider,
                               A.deleted_at.is_(None))
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
        # Same org may send from several accounts: only this account's logs.
        log = (base.filter(L.phone_e164 == phone,
                           or_(L.account_id == account.id, L.account_id.is_(None)),
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
    from app.whatsapp_layer import flow_alerts
    from app.whatsapp_layer import template_status as TS
    changed: List[int] = []
    now = datetime.utcnow()
    for ev in events:
        if ev.kind == "inbound":
            from app.whatsapp_layer import today as today_list
            if ev.recipient and (ev.text or "").strip().lower() in today_list.SHOW_LIST_WORDS:
                changed.extend(today_list.send_list_reply(db, account, ev.recipient,
                                                          ev.provider_message_id))
        elif ev.kind == "message_status":
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
        elif ev.kind in ("template_status", "template_quality", "template_category"):
            tpl = TS.find_template(db, account, ev.provider_template_id, ev.template_name,
                                   ev.template_language)
            if tpl is None:
                logger.info("Webhook: %s for unknown template %s (%s)", ev.kind,
                            ev.template_name, ev.provider_template_id)
                continue
            notice = None
            if ev.kind == "template_category" and (ev.event or "").startswith("CORRECT_CATEGORY:"):
                notice = ev.event.split(":", 1)[1]
            TS.apply_change(db, tpl, TS.TemplateChange(
                status=ev.status if ev.kind == "template_status" else None,
                event=ev.event, reason=ev.reason,
                category=ev.category if ev.kind == "template_category" else None,
                previous_category=ev.previous_category,
                quality_score=ev.quality_score, category_notice=notice, source="webhook"))
        elif ev.kind == "flow_event":
            flow_alerts.handle(db, account, ev)
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
        try:
            from app.whatsapp_layer import campaign_forward
            campaign_forward.forward(account.organization_id, headers, raw, events)
        except Exception as exc:  # forwarding must never block our own processing
            logger.warning("Campaign forward skipped: %s", exc)
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
