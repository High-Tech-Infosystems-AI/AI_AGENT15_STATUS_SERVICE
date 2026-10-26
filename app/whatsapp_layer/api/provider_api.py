"""Providers + provider accounts — /status/whatsapp/providers, /accounts (contract §2.1).

Permission: whatsapp_settings.manage (admin tier bypasses).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx, resolve_read_org
from app.whatsapp_layer import accounts as acct_svc, crypto, people, sender
from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import variables as V
from app.whatsapp_layer.api.common import get_scoped, unprocessable, write_org
from app.whatsapp_layer.models import WhatsAppProviderAccount as A, WhatsAppTemplate as T
from app.whatsapp_layer.providers.registry import PROVIDER_CATALOGUE, available_keys
from app.whatsapp_layer.schemas import AccountCreate, AccountTest, AccountUpdate

logger = logging.getLogger("app_logger")
router = APIRouter()

PERM = "whatsapp_settings.manage"


def _encrypt(token: str) -> bytes:
    try:
        return crypto.encrypt(token)
    except RuntimeError as exc:
        logger.error("WhatsApp token encryption unavailable: %s", exc)
        raise HTTPException(500, "WHATSAPP_CRED_KEY is not configured on the server")


@router.get("/providers")
def list_providers(ctx: AuthCtx = Depends(get_auth_ctx)):
    ctx.require(PERM)
    return PROVIDER_CATALOGUE


@router.get("/accounts")
def list_accounts(organization_id: Optional[int] = Query(None),
                  ctx: AuthCtx = Depends(get_auth_ctx),
                  db: Session = Depends(get_db)):
    ctx.require(PERM)
    org = resolve_read_org(organization_id, ctx)
    q = db.query(A)
    if org is not None:
        q = q.filter(A.organization_id == org)
    return {"data": [acct_svc.serialize(a) for a in q.order_by(A.id.asc()).all()]}


@router.post("/accounts")
def create_account(body: AccountCreate,
                   organization_id: Optional[int] = Query(None),
                   ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    ctx.require(PERM)
    org = write_org(organization_id, body.organization_id, ctx)
    if body.provider not in available_keys():
        unprocessable([{"field": "provider", "message": f"Provider '{body.provider}' is not available"}])
    if db.query(A.id).filter(A.organization_id == org, A.provider == body.provider).first():
        raise HTTPException(409, "This organisation already has an account for this provider")

    acct = A(organization_id=org, provider=body.provider, display_name=body.display_name.strip(),
             base_url=body.base_url.strip().rstrip("/"), username=body.username,
             access_token_enc=_encrypt(body.access_token.strip()),
             token_last4=crypto.last4(body.access_token), waba_id=body.waba_id,
             phone_number_id=body.phone_number_id,
             display_phone_number=body.display_phone_number,
             webhook_token=acct_svc.new_webhook_token(), status="active", sandbox=0,
             created_by=ctx.user_id, updated_by=ctx.user_id)
    db.add(acct)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This organisation already has an account for this provider")
    # Seeded / draft templates created before the account existed belong to it now.
    (db.query(T).filter(T.organization_id == org, T.account_id.is_(None))
     .update({T.account_id: acct.id}, synchronize_session=False))
    db.commit()
    db.refresh(acct)
    logger.info("WhatsApp account %s created for org %s by user %s", acct.id, org, ctx.user_id)
    return acct_svc.serialize(acct)


@router.put("/accounts/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    ctx.require(PERM)
    acct = get_scoped(db, A, account_id, ctx, soft_delete=False)
    data = body.model_dump(exclude_unset=True)
    if "provider" in data and data["provider"] != acct.provider:
        if data["provider"] not in available_keys():
            unprocessable([{"field": "provider", "message": f"Provider '{data['provider']}' is not available"}])
        acct.provider = data["provider"]
    if "status" in data and data["status"] is not None:
        if data["status"] not in ("active", "disabled"):
            unprocessable([{"field": "status", "message": "Must be active or disabled"}])
        acct.status = data["status"]
    for field in ("display_name", "username", "waba_id", "phone_number_id", "display_phone_number"):
        if field in data:
            setattr(acct, field, data[field])
    if data.get("base_url"):
        acct.base_url = data["base_url"].strip().rstrip("/")
    if data.get("access_token"):
        acct.access_token_enc = _encrypt(data["access_token"].strip())
        acct.token_last4 = crypto.last4(data["access_token"])
        # A new token clears a previous auth error.
        if acct.status == "auth_error" and data.get("status") is None:
            acct.status = "active"
        acct.last_error = None
    acct.updated_by = ctx.user_id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This organisation already has an account for this provider")
    db.refresh(acct)
    return acct_svc.serialize(acct)


@router.post("/accounts/{account_id}/health")
def refresh_account_health(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                           db: Session = Depends(get_db)):
    ctx.require(PERM)
    acct = get_scoped(db, A, account_id, ctx, soft_delete=False)
    acct_svc.refresh_health(db, acct)
    db.commit()
    db.refresh(acct)
    return acct_svc.serialize(acct)


@router.post("/accounts/{account_id}/rotate-webhook-token")
def rotate_webhook_token(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                         db: Session = Depends(get_db)):
    ctx.require(PERM)
    acct = get_scoped(db, A, account_id, ctx, soft_delete=False)
    acct.webhook_token = acct_svc.new_webhook_token()
    acct.updated_by = ctx.user_id
    db.commit()
    db.refresh(acct)
    logger.info("Webhook token rotated for WhatsApp account %s by user %s", acct.id, ctx.user_id)
    return acct_svc.serialize(acct)


@router.post("/accounts/{account_id}/test")
def send_test_message(account_id: int, body: Optional[AccountTest] = None,
                      ctx: AuthCtx = Depends(get_auth_ctx),
                      db: Session = Depends(get_db)):
    """Health check + a test message (defaults: caller's own phone, first
    approved template)."""
    ctx.require(PERM)
    body = body or AccountTest()
    acct = get_scoped(db, A, account_id, ctx, soft_delete=False)
    org = acct.organization_id

    if body.template_id:
        tpl = db.query(T).filter(T.id == body.template_id, T.organization_id == org,
                                 T.deleted_at.is_(None)).first()
        if tpl is None:
            raise HTTPException(404, "Template not found")
    else:
        tpl = (db.query(T).filter(T.organization_id == org, T.approval_status == "approved",
                                  T.is_active == 1, T.deleted_at.is_(None))
               .order_by(T.id.asc()).first())
        if tpl is None:
            raise HTTPException(409, "No approved template to send a test with")

    recipient_id, recipient_name = None, None
    if body.phone_number:
        phone = people.to_e164(body.phone_country_code, body.phone_number)
        if not phone:
            unprocessable([{"field": "phone_number", "message": "Not a valid phone number"}])
    else:
        me = people.get_users(db, [ctx.user_id]).get(ctx.user_id) or {}
        phone, recipient_id, recipient_name = me.get("phone_e164"), ctx.user_id, me.get("name")

    # Best-effort health refresh first (plan §8.1: "health check plus a test").
    try:
        acct_svc.refresh_health(db, acct)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Health refresh before test send failed: %s", exc)

    params = {k: V.sample_for(k) or k for k in V.VARIABLES_BY_KEY}
    params.update(tpl.sample_values or {})
    st = E.get_settings(db, org)
    log, _ = sender.create_log(
        db, organization_id=org, kind="test", triggered_by="manual", created_by=ctx.user_id,
        idempotency_key=f"test_{uuid.uuid4().hex}",  # tests are never deduplicated
        account_id=acct.id, provider=acct.provider, template_id=tpl.id, template_name=tpl.name,
        recipient_user_id=recipient_id, recipient_name=recipient_name, recipient_type="recruiter",
        phone_e164=phone, params=params)
    sender.apply_prechecks(db, log, settings=st, account=acct, template=tpl,
                           user={"enabled": True, "phone_e164": phone})
    db.commit()
    if log.status == "queued":
        log = sender.send_log(db, log.id) or log
    db.refresh(log)
    return sender.serialize_log(log)
