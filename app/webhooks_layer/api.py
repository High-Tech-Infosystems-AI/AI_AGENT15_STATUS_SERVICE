"""Outgoing webhook endpoints — /status/webhooks.

Permission: developer.manage or api_keys.manage (admin tier bypasses), the
same gate as API keys in the Developer Portal.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.webhooks_layer import catalog, delivery
from app.webhooks_layer.models import WebhookDelivery as D, WebhookEndpoint as E
from app.webhooks_layer.schemas import EndpointCreate, EndpointUpdate
from app.webhooks_layer.urls import url_problem
from app.whatsapp_layer import crypto
from app.whatsapp_layer.api.common import get_scoped, paginate, read_org, unprocessable, write_org

logger = logging.getLogger("app_logger")
router = APIRouter()

PERMS = ["developer.manage", "api_keys.manage"]
DELIVERY_STATUSES = ("pending", "sending", "retrying", "succeeded", "failed")


def _check_events(events) -> list:
    events = sorted(set(events or []))
    unknown = [e for e in events if e != "*" and e not in catalog.SUBSCRIBABLE]
    if unknown:
        unprocessable([{"field": "events", "message": f"Unknown events: {', '.join(unknown)}"}])
    return ["*"] if "*" in events else events


def _check_url(url: str) -> str:
    url = url.strip()
    problem = url_problem(url)
    if problem:
        unprocessable([{"field": "url", "message": problem}])
    return url


def _encrypt(secret: str) -> bytes:
    try:
        return crypto.encrypt(secret)
    except RuntimeError as exc:
        logger.error("Webhook secret encryption unavailable: %s", exc)
        raise HTTPException(500, "WHATSAPP_CRED_KEY is not configured on the server")


def _endpoint_out(e: E, secret: Optional[str] = None) -> dict:
    out = {
        "id": e.id, "organization_id": e.organization_id, "name": e.name,
        "description": e.description, "url": e.url, "events": e.events or [],
        "is_active": bool(e.is_active), "disabled_reason": e.disabled_reason,
        "secret_last4": e.secret_last4, "consecutive_failures": e.consecutive_failures or 0,
        "last_delivery_at": e.last_delivery_at, "last_success_at": e.last_success_at,
        "created_by": e.created_by, "created_at": e.created_at, "updated_at": e.updated_at,
    }
    if secret is not None:
        out["secret"] = secret  # shown once, on create and rotate
    return out


def _delivery_out(d: D, full: bool = False) -> dict:
    out = {
        "id": d.id, "endpoint_id": d.endpoint_id, "event_id": d.event_id,
        "event_name": d.event_name, "status": d.status, "attempts": d.attempts,
        "max_attempts": d.max_attempts, "next_attempt_at": d.next_attempt_at,
        "response_status": d.response_status, "error": d.error, "duration_ms": d.duration_ms,
        "created_at": d.created_at, "delivered_at": d.delivered_at,
    }
    if full:
        try:
            out["payload"] = json.loads(d.payload)
        except ValueError:
            out["payload"] = d.payload
        out["response_body"] = d.response_body
    return out


@router.get("/events")
def list_events(ctx: AuthCtx = Depends(get_auth_ctx)):
    ctx.require_any(PERMS)
    return {"groups": catalog.as_list(), "signature": {
        "header": "X-HRMIS-Signature", "timestamp_header": "X-HRMIS-Timestamp",
        "scheme": "sha256=hex(HMAC_SHA256(secret, timestamp + '.' + raw_body))"}}


@router.get("/endpoints")
def list_endpoints(organization_id: Optional[int] = Query(None),
                   ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    org = read_org(organization_id, ctx)
    rows = (db.query(E).filter(E.organization_id == org, E.deleted_at.is_(None))
            .order_by(E.created_at.desc(), E.id.desc()).all())
    return [_endpoint_out(e) for e in rows]


@router.post("/endpoints", status_code=201)
def create_endpoint(body: EndpointCreate, organization_id: Optional[int] = Query(None),
                    ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    org = write_org(organization_id, body.organization_id, ctx)
    secret = delivery.new_secret()
    e = E(organization_id=org, name=body.name.strip(), description=body.description,
          url=_check_url(body.url), events=_check_events(body.events),
          is_active=1 if body.is_active else 0, secret_enc=_encrypt(secret),
          secret_last4=secret[-4:], consecutive_failures=0, created_by=ctx.user_id)
    db.add(e)
    db.commit()
    db.refresh(e)
    return _endpoint_out(e, secret=secret)


@router.get("/endpoints/{endpoint_id}")
def get_endpoint(endpoint_id: int, ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    return _endpoint_out(get_scoped(db, E, endpoint_id, ctx))


@router.put("/endpoints/{endpoint_id}")
def update_endpoint(endpoint_id: int, body: EndpointUpdate,
                    ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    e = get_scoped(db, E, endpoint_id, ctx)
    if body.name is not None:
        e.name = body.name.strip()
    if body.description is not None:
        e.description = body.description
    if body.url is not None:
        e.url = _check_url(body.url)
    if body.events is not None:
        e.events = _check_events(body.events)
    if body.is_active is not None:
        e.is_active = 1 if body.is_active else 0
        if body.is_active:
            # Switching back on starts the failure count afresh.
            e.disabled_reason, e.consecutive_failures = None, 0
    db.commit()
    db.refresh(e)
    return _endpoint_out(e)


@router.delete("/endpoints/{endpoint_id}", status_code=204)
def delete_endpoint(endpoint_id: int, ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    e = get_scoped(db, E, endpoint_id, ctx)
    e.deleted_at, e.is_active = datetime.utcnow(), 0
    db.commit()


@router.post("/endpoints/{endpoint_id}/rotate-secret")
def rotate_secret(endpoint_id: int, ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    e = get_scoped(db, E, endpoint_id, ctx)
    secret = delivery.new_secret()
    e.secret_enc, e.secret_last4 = _encrypt(secret), secret[-4:]
    db.commit()
    db.refresh(e)
    return _endpoint_out(e, secret=secret)


@router.post("/endpoints/{endpoint_id}/test")
def send_test(endpoint_id: int, ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    e = get_scoped(db, E, endpoint_id, ctx)
    return _delivery_out(delivery.ping(db, e), full=True)


@router.get("/endpoints/{endpoint_id}/deliveries")
def list_deliveries(endpoint_id: int, status: Optional[str] = Query(None),
                    event_name: Optional[str] = Query(None),
                    page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100),
                    ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    e = get_scoped(db, E, endpoint_id, ctx, soft_delete=False)
    q = db.query(D).filter(D.endpoint_id == e.id)
    if status:
        if status not in DELIVERY_STATUSES:
            unprocessable([{"field": "status", "message": f"Use one of: {', '.join(DELIVERY_STATUSES)}"}])
        q = q.filter(D.status == status)
    if event_name:
        q = q.filter(D.event_name == event_name)
    rows, meta = paginate(q.order_by(D.created_at.desc(), D.id.desc()), page, limit)
    return {"items": [_delivery_out(d) for d in rows], **meta}


@router.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: int, ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    return _delivery_out(get_scoped(db, D, delivery_id, ctx, soft_delete=False), full=True)


@router.post("/deliveries/{delivery_id}/redeliver", status_code=202)
def redeliver(delivery_id: int, background_tasks: BackgroundTasks,
              ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require_any(PERMS)
    d = get_scoped(db, D, delivery_id, ctx, soft_delete=False)
    e = get_scoped(db, E, d.endpoint_id, ctx)
    if not e.is_active:
        raise HTTPException(409, "Switch the endpoint on before redelivering.")
    row = delivery.redeliver(db, d)
    background_tasks.add_task(delivery.deliver_now, [row.id])
    return _delivery_out(row, full=True)
