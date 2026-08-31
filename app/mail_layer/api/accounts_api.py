"""Account / configuration endpoints."""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.mail_layer import crypto, imap_sync, models as m, smtp_send, store, tasks
from app.mail_layer.providers import PROVIDERS, list_providers
from app.mail_layer.schemas import (
    AccountCreate, AccountOut, AccountUpdate, ConnStep,
    TestConnectionRequest, TestConnectionResult,
)

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.get("/providers")
def get_providers(ctx: AuthCtx = Depends(get_auth_ctx)):
    return list_providers()


@router.post("/accounts", response_model=AccountOut)
def create_account(body: AccountCreate,
                   ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    if not body.consent_acknowledged:
        raise HTTPException(400, "Full-mirror consent must be acknowledged")

    email = str(body.email_address).strip()
    smtp_user = (body.smtp_username or email).strip()
    imap_user = (body.imap_username or email).strip()

    # The unique key uq_user_email (user_id, email_address) also counts
    # soft-deleted rows, so we must look past deleted_at here: if the user
    # previously removed this mailbox (soft delete), reactivate that row instead
    # of inserting a duplicate — otherwise db.flush() raises IntegrityError (500).
    existing = (db.query(m.MailAccount)
                .filter(m.MailAccount.user_id == ctx.user_id,
                        m.MailAccount.email_address == email)
                .first())
    if existing and existing.deleted_at is None:
        raise HTTPException(409, "This mailbox is already connected")

    acct = existing or m.MailAccount(user_id=ctx.user_id)
    acct.deleted_at = None
    acct.organization_id = ctx.organization_id
    acct.display_name = body.display_name
    acct.email_address = email
    acct.provider = body.provider
    acct.smtp_host = body.smtp_host
    acct.smtp_port = body.smtp_port
    acct.smtp_security = body.smtp_security
    acct.smtp_username = smtp_user
    acct.smtp_password_enc = crypto.encrypt(body.smtp_password)
    acct.imap_host = body.imap_host
    acct.imap_port = body.imap_port
    acct.imap_security = body.imap_security
    acct.imap_username = imap_user
    acct.imap_password_enc = (None if body.use_same_credentials
                              else crypto.encrypt(body.imap_password or ""))
    acct.use_same_credentials = 1 if body.use_same_credentials else 0
    acct.sync_enabled = 1 if body.sync_enabled else 0
    acct.sync_interval_seconds = body.sync_interval_seconds
    acct.backfill_days = body.backfill_days
    acct.use_idle = 1 if body.use_idle else 0
    acct.status = "pending"
    acct.consent_acknowledged = 1
    acct.is_default = 1
    if existing is None:
        db.add(acct)

    try:
        db.flush()
        store.seed_system_folders(db, acct)  # idempotent
        store.ensure_settings(db, acct)      # idempotent
        db.commit()
    except IntegrityError:
        # A concurrent double-submit raced us to the same (user, email).
        db.rollback()
        raise HTTPException(409, "This mailbox is already connected")

    db.refresh(acct)

    # Kick the first mirror off-thread so the request returns immediately.
    tasks.trigger_background_sync(acct.id)
    return store.serialize_account(acct)


@router.get("/accounts", response_model=List[AccountOut])
def list_accounts(ctx: AuthCtx = Depends(get_auth_ctx),
                  db: Session = Depends(get_db)):
    rows = (db.query(m.MailAccount)
            .filter(m.MailAccount.user_id == ctx.user_id,
                    m.MailAccount.deleted_at.is_(None))
            .order_by(m.MailAccount.is_default.desc(), m.MailAccount.id.asc()).all())
    return [store.serialize_account(a) for a in rows]


@router.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    return store.serialize_account(store.get_owned_account(db, ctx.user_id, account_id))


@router.put("/accounts/{account_id}", response_model=AccountOut)
def update_account(account_id: int, body: AccountUpdate,
                   ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    acct = store.get_owned_account(db, ctx.user_id, account_id)
    data = body.model_dump(exclude_unset=True)
    for field in ("display_name", "provider", "smtp_host", "smtp_port",
                  "smtp_security", "smtp_username", "imap_host", "imap_port",
                  "imap_security", "imap_username", "sync_interval_seconds",
                  "backfill_days"):
        if field in data and data[field] is not None:
            setattr(acct, field, data[field])
    for bfield in ("use_same_credentials", "sync_enabled", "use_idle"):
        if bfield in data and data[bfield] is not None:
            setattr(acct, bfield, 1 if data[bfield] else 0)
    # Trim stray whitespace that would break SMTP/IMAP login.
    for sfield in ("smtp_username", "imap_username", "smtp_host", "imap_host"):
        val = getattr(acct, sfield, None)
        if isinstance(val, str):
            setattr(acct, sfield, val.strip())
    if data.get("smtp_password"):
        acct.smtp_password_enc = crypto.encrypt(data["smtp_password"])
    if data.get("imap_password"):
        acct.imap_password_enc = crypto.encrypt(data["imap_password"])
    if acct.use_same_credentials:
        acct.imap_password_enc = None
    db.commit()
    db.refresh(acct)
    return store.serialize_account(acct)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, purge: bool = Query(False),
                   ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    from datetime import datetime
    acct = store.get_owned_account(db, ctx.user_id, account_id)
    acct.deleted_at = datetime.utcnow()
    acct.status = "disconnected"
    acct.sync_enabled = 0
    if purge:
        # ON DELETE CASCADE handles folders/messages/attachments/settings.
        db.delete(acct)
    db.commit()
    return {"ok": True, "purged": purge}


@router.post("/accounts/test-connection", response_model=TestConnectionResult)
def test_connection(body: TestConnectionRequest,
                    ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    smtp_pw = body.smtp_password
    imap_pw = body.imap_password
    imap_user = body.imap_username or body.smtp_username
    if body.account_id:
        acct = store.get_owned_account(db, ctx.user_id, body.account_id)
        if not smtp_pw:
            smtp_pw = crypto.decrypt(acct.smtp_password_enc)
        if body.use_same_credentials:
            imap_pw = smtp_pw
        elif not imap_pw and acct.imap_password_enc:
            imap_pw = crypto.decrypt(acct.imap_password_enc)
    elif body.use_same_credentials:
        imap_pw = smtp_pw

    steps: List[ConnStep] = []
    ok_smtp, detail_smtp = smtp_send.test_smtp(
        body.smtp_host, body.smtp_port, body.smtp_security,
        body.smtp_username, smtp_pw or "")
    steps.append(ConnStep(step="SMTP login", ok=ok_smtp, detail=detail_smtp))

    ok_imap, detail_imap, folders = imap_sync.test_imap(
        body.imap_host, body.imap_port, body.imap_security,
        imap_user or body.smtp_username, imap_pw or "")
    steps.append(ConnStep(step="IMAP login", ok=ok_imap, detail=detail_imap))
    if ok_imap:
        steps.append(ConnStep(step="List folders", ok=True,
                              detail=f"{folders} folders"))

    return TestConnectionResult(ok=ok_smtp and ok_imap, steps=steps,
                                folders_found=folders)


@router.post("/accounts/{account_id}/resync")
def resync(account_id: int, full: bool = Query(False),
           ctx: AuthCtx = Depends(get_auth_ctx),
           db: Session = Depends(get_db)):
    acct = store.get_owned_account(db, ctx.user_id, account_id)
    if full:
        db.query(m.MailFolder).filter(m.MailFolder.account_id == acct.id).update(
            {"last_uid": 0, "uidvalidity": None})
        db.commit()
    tasks.trigger_background_sync(acct.id)
    return {"ok": True, "message": "Sync started"}


@router.get("/accounts/{account_id}/sync-status")
def sync_status(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    acct = store.get_owned_account(db, ctx.user_id, account_id)
    return {
        "status": acct.status, "last_synced_at": acct.last_synced_at,
        "last_error": acct.last_error, "sync_enabled": bool(acct.sync_enabled),
    }
