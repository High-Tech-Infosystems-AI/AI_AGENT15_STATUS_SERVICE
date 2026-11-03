"""WhatsApp message logs, retry and send-now —
/status/clawback-notifications/logs, /send-now (contract §2.8).

Permissions: notification_logs.view (read) / clawback_notifications.manage
(retry, send-now).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx, resolve_read_org
from app.whatsapp_layer import clients, sender
from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer.api.common import get_scoped, paginate, write_org
from app.whatsapp_layer.models import WhatsAppMessageLog as L
from app.whatsapp_layer.schemas import SendNow

logger = logging.getLogger("app_logger")
router = APIRouter()

VIEW = "notification_logs.view"
MANAGE = "clawback_notifications.manage"


@router.get("/logs")
def list_logs(kind: Optional[str] = Query(None), status: Optional[str] = Query(None),
              recipient_user_id: Optional[int] = Query(None), job_id: Optional[int] = Query(None),
              candidate_job_id: Optional[int] = Query(None),
              date_from: Optional[date] = Query(None), date_to: Optional[date] = Query(None),
              search: Optional[str] = Query(None),
              page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100),
              organization_id: Optional[int] = Query(None),
              ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    org = resolve_read_org(organization_id, ctx)
    q = db.query(L)
    if org is not None:
        q = q.filter(L.organization_id == org)
    if kind:
        q = q.filter(L.kind == kind)
    if recipient_user_id:
        q = q.filter(L.recipient_user_id == recipient_user_id)
    if job_id:
        q = q.filter(L.job_id == job_id)
    if candidate_job_id:
        q = q.filter(L.candidate_job_id == candidate_job_id)
    # Date range on created_at (UTC dates, inclusive).
    if date_from:
        q = q.filter(L.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        q = q.filter(L.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(L.candidate_name.like(like), L.recipient_name.like(like),
                         L.job_title.like(like), L.template_name.like(like),
                         L.phone_e164.like(like), L.candidate_id.like(like)))

    # Summary counts follow every filter except `status` itself.
    summary = {s: 0 for s in sender.SUMMARY_STATUSES}
    for st, n in (q.with_entities(L.status, func.count(L.id)).group_by(L.status).all()):
        summary[st] = int(n)
    if status:
        q = q.filter(L.status == status)
    rows, pagination = paginate(q.order_by(L.id.desc()), page, limit)
    return {"data": [sender.serialize_log(r) for r in rows], "pagination": pagination,
            "summary": summary}


@router.get("/logs/{log_id}")
def get_log(log_id: int, ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    return sender.serialize_log(get_scoped(db, L, log_id, ctx, soft_delete=False))


@router.post("/logs/{log_id}/retry")
def retry_log(log_id: int, ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    """A new attempt for a failed message (same log row; attempts keeps counting)."""
    ctx.require(MANAGE)
    log = get_scoped(db, L, log_id, ctx, soft_delete=False)
    if log.status != "failed":
        raise HTTPException(409, "Only failed messages can be retried")
    log.status = "queued"
    log.triggered_by = "retry"
    log.error_code = log.error_message = None
    log.failed_at = log.next_attempt_at = None
    db.commit()
    log = sender.send_log(db, log.id) or log
    db.refresh(log)
    logger.info("WhatsApp log %s retried by user %s → %s", log.id, ctx.user_id, log.status)
    return sender.serialize_log(log)


@router.post("/send-now")
def send_now(body: SendNow, organization_id: Optional[int] = Query(None),
             ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    org = write_org(organization_id, body.organization_id, ctx)
    try:
        log = E.send_now(db, org, body.candidate_job_id, ctx.user_id)
    except E.ClawbackNotFound as exc:
        raise HTTPException(404, str(exc))
    except clients.ServiceClientError as exc:
        raise HTTPException(502, str(exc))
    db.refresh(log)
    return sender.serialize_log(log)
