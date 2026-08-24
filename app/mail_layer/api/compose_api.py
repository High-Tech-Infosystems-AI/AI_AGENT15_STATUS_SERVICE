"""Compose, drafts, outbox and send endpoints.

Sending is decoupled: the API moves a message into the Outbox with
send_status='queued'; the send_worker drains it via SMTP and files the copy in
Sent. This keeps the request fast and gives the undo-send window somewhere to
live.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.mail_layer import models as m, parsing, store
from app.mail_layer.schemas import (
    DraftCreate, DraftUpdate, MessageOut, ScheduleRequest, SendRequest,
)

router = APIRouter()


def _addrs(values: List[str]) -> list:
    return [{"name": "", "address": v.strip().lower()} for v in (values or []) if v and v.strip()]


def _require_account(db: Session, user_id: int) -> m.MailAccount:
    acct = store.get_default_account(db, user_id)
    if not acct:
        raise HTTPException(400, "Connect a mailbox first")
    return acct


def _draft_folder(db: Session, account_id: int) -> m.MailFolder:
    f = store.get_folder_by_role(db, account_id, "drafts")
    if not f:
        raise HTTPException(400, "No drafts folder")
    return f


def _apply_compose_fields(msg: m.MailMessage, body) -> None:
    msg.to_json = _addrs(body.to)
    msg.cc_json = _addrs(body.cc)
    msg.bcc_json = _addrs(body.bcc)
    msg.subject = (body.subject or "")[:998]
    msg.body_html = body.body_html
    msg.body_text = body.body_text or (parsing.html_to_text(body.body_html or ""))
    msg.snippet = parsing.make_snippet(msg.body_text)
    msg.importance = body.importance or "normal"
    msg.in_reply_to = body.in_reply_to


# ------------------------------------------------------------------ drafts
@router.post("/drafts", response_model=MessageOut)
def create_draft(body: DraftCreate, ctx: AuthCtx = Depends(get_auth_ctx),
                 db: Session = Depends(get_db)):
    acct = _require_account(db, ctx.user_id)
    drafts = _draft_folder(db, acct.id)
    msg = m.MailMessage(
        account_id=acct.id, folder_id=drafts.id, user_id=ctx.user_id,
        organization_id=ctx.organization_id, direction="out", is_draft=1,
        from_name=acct.display_name, from_address=acct.email_address, is_read=1)
    _apply_compose_fields(msg, body)
    db.add(msg)
    db.flush()
    store.refresh_folder_counts(db, drafts.id)
    db.commit()
    db.refresh(msg)
    return store.serialize_message_full(db, msg)


@router.put("/drafts/{draft_id}", response_model=MessageOut)
def update_draft(draft_id: int, body: DraftUpdate,
                 ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, draft_id)
    if not msg.is_draft:
        raise HTTPException(400, "Not a draft")
    _apply_compose_fields(msg, body)
    db.commit()
    db.refresh(msg)
    return store.serialize_message_full(db, msg)


@router.delete("/drafts/{draft_id}")
def delete_draft(draft_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                 db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, draft_id)
    if not msg.is_draft:
        raise HTTPException(400, "Not a draft")
    fid = msg.folder_id
    msg.deleted_at = datetime.utcnow()
    store.refresh_folder_counts(db, fid)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ send
def _enqueue(db: Session, ctx: AuthCtx, msg: m.MailMessage,
             scheduled_at: Optional[datetime] = None) -> None:
    outbox = store.get_folder_by_role(db, msg.account_id, "outbox")
    if not outbox:
        raise HTTPException(400, "No outbox folder")
    if not (msg.to_json or msg.cc_json or msg.bcc_json):
        raise HTTPException(400, "At least one recipient is required")
    msg.is_draft = 0
    msg.send_status = "queued"
    msg.send_attempts = 0
    msg.send_error = None
    msg.scheduled_at = scheduled_at
    store.move_message(db, msg, outbox.id)


@router.post("/messages/send")
def send_message(body: SendRequest, ctx: AuthCtx = Depends(get_auth_ctx),
                 db: Session = Depends(get_db)):
    if body.draft_id:
        msg = store.get_owned_message(db, ctx.user_id, body.draft_id)
        # allow re-applying edited fields sent alongside draft_id
        if body.to or body.subject or body.body_html or body.body_text:
            _apply_compose_fields(msg, body)
    else:
        acct = _require_account(db, ctx.user_id)
        drafts = _draft_folder(db, acct.id)
        msg = m.MailMessage(
            account_id=acct.id, folder_id=drafts.id, user_id=ctx.user_id,
            organization_id=ctx.organization_id, direction="out", is_read=1,
            from_name=acct.display_name, from_address=acct.email_address)
        _apply_compose_fields(msg, body)
        db.add(msg)
        db.flush()
    _enqueue(db, ctx, msg)
    db.commit()
    return {"ok": True, "message_id": msg.id, "status": "queued"}


@router.post("/drafts/{draft_id}/send")
def send_draft(draft_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
               db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, draft_id)
    _enqueue(db, ctx, msg)
    db.commit()
    return {"ok": True, "message_id": msg.id, "status": "queued"}


@router.post("/messages/schedule")
def schedule_message(body: ScheduleRequest, ctx: AuthCtx = Depends(get_auth_ctx),
                     db: Session = Depends(get_db)):
    if body.draft_id:
        msg = store.get_owned_message(db, ctx.user_id, body.draft_id)
        _apply_compose_fields(msg, body)
    else:
        acct = _require_account(db, ctx.user_id)
        drafts = _draft_folder(db, acct.id)
        msg = m.MailMessage(
            account_id=acct.id, folder_id=drafts.id, user_id=ctx.user_id,
            organization_id=ctx.organization_id, direction="out", is_read=1,
            from_name=acct.display_name, from_address=acct.email_address)
        _apply_compose_fields(msg, body)
        db.add(msg)
        db.flush()
    _enqueue(db, ctx, msg, scheduled_at=body.scheduled_at)
    db.commit()
    return {"ok": True, "message_id": msg.id, "scheduled_at": body.scheduled_at}


# ------------------------------------------------------------------ outbox
@router.get("/outbox", response_model=List[MessageOut])
def list_outbox(ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    rows = (db.query(m.MailMessage)
            .filter(m.MailMessage.user_id == ctx.user_id,
                    m.MailMessage.send_status.in_(["queued", "sending", "failed"]),
                    m.MailMessage.deleted_at.is_(None))
            .order_by(m.MailMessage.created_at.desc()).all())
    return [store.serialize_message_full(db, msg) for msg in rows]


@router.post("/outbox/{message_id}/cancel")
def cancel_send(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    """Undo send — pull a queued/scheduled/failed message back to Drafts."""
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    if msg.send_status not in ("queued", "failed"):
        raise HTTPException(409, f"Cannot cancel a message that is '{msg.send_status}'")
    drafts = _draft_folder(db, msg.account_id)
    msg.send_status = "canceled"
    msg.scheduled_at = None
    msg.is_draft = 1
    store.move_message(db, msg, drafts.id)
    db.commit()
    return {"ok": True, "status": "returned to drafts"}


# ------------------------------------------------------------------ reply / forward
def _quote(msg: m.MailMessage) -> str:
    when = msg.sent_at or msg.internal_date or msg.created_at
    header = f"On {when}, {msg.from_name or msg.from_address} wrote:"
    body = msg.body_html or (f"<pre>{msg.body_text or ''}</pre>")
    return f"<br><br><blockquote>{header}<br>{body}</blockquote>"


@router.post("/messages/{message_id}/reply", response_model=MessageOut)
def reply(message_id: int, all: bool = False, ctx: AuthCtx = Depends(get_auth_ctx),
          db: Session = Depends(get_db)):
    src = store.get_owned_message(db, ctx.user_id, message_id)
    acct = store.get_owned_account(db, ctx.user_id, src.account_id)
    drafts = _draft_folder(db, acct.id)
    to = [src.reply_to_address or src.from_address] if (src.from_address or src.reply_to_address) else []
    cc = []
    if all:
        cc = [a.get("address") for a in (src.cc_json or []) if a.get("address")]
        cc += [a.get("address") for a in (src.to_json or [])
               if a.get("address") and a.get("address") != acct.email_address]
    subj = src.subject or ""
    if not subj.lower().startswith("re:"):
        subj = f"Re: {subj}"
    msg = m.MailMessage(
        account_id=acct.id, folder_id=drafts.id, user_id=ctx.user_id,
        organization_id=ctx.organization_id, direction="out", is_draft=1, is_read=1,
        from_name=acct.display_name, from_address=acct.email_address,
        to_json=_addrs(to), cc_json=_addrs(cc), subject=subj[:998],
        in_reply_to=src.message_id_hdr, conversation_id=src.conversation_id,
        body_html=_quote(src), body_text="")
    msg.snippet = ""
    db.add(msg)
    db.flush()
    store.refresh_folder_counts(db, drafts.id)
    db.commit()
    db.refresh(msg)
    return store.serialize_message_full(db, msg)


@router.post("/messages/{message_id}/forward", response_model=MessageOut)
def forward(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
            db: Session = Depends(get_db)):
    src = store.get_owned_message(db, ctx.user_id, message_id)
    acct = store.get_owned_account(db, ctx.user_id, src.account_id)
    drafts = _draft_folder(db, acct.id)
    subj = src.subject or ""
    if not subj.lower().startswith("fw:"):
        subj = f"Fw: {subj}"
    msg = m.MailMessage(
        account_id=acct.id, folder_id=drafts.id, user_id=ctx.user_id,
        organization_id=ctx.organization_id, direction="out", is_draft=1, is_read=1,
        is_forwarded=1, from_name=acct.display_name, from_address=acct.email_address,
        subject=subj[:998], body_html=_quote(src), body_text="")
    db.add(msg)
    db.flush()
    # copy attachment metadata onto the forward draft
    for att in src.attachments:
        db.add(m.MailAttachment(
            message_id=msg.id, account_id=acct.id, user_id=ctx.user_id,
            s3_key=att.s3_key, content_id=att.content_id, file_name=att.file_name,
            mime_type=att.mime_type, size_bytes=att.size_bytes,
            is_inline=att.is_inline, fetched=att.fetched))
    if src.attachments:
        msg.has_attachments = 1
    store.refresh_folder_counts(db, drafts.id)
    db.commit()
    db.refresh(msg)
    return store.serialize_message_full(db, msg)
