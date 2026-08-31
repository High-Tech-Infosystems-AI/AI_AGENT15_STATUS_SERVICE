"""Message list / read / action endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, defer

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.mail_layer import models as m, store
from app.mail_layer.schemas import (
    BulkRequest, CategoriesRequest, MessageListItem, MessageOut, MoveRequest,
)

router = APIRouter()

# Heavy MEDIUMTEXT columns not needed for an envelope list — deferring them stops
# the list query from hauling every message's full body/html over the wire.
_LIST_DEFER = (
    defer(m.MailMessage.body_text),
    defer(m.MailMessage.body_html),
    defer(m.MailMessage.references_hdr),
)


def _settings_for(db: Session, account_id: int) -> Optional[m.MailSettings]:
    return db.query(m.MailSettings).filter(
        m.MailSettings.account_id == account_id).first()


# ------------------------------------------------------------------ list
@router.get("/folders/{folder_id}/messages", response_model=List[MessageListItem])
def list_messages(folder_id: int,
                  page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
                  q: Optional[str] = Query(None),
                  unread_only: bool = Query(False),
                  flagged_only: bool = Query(False),
                  category_id: Optional[int] = Query(None),
                  ctx: AuthCtx = Depends(get_auth_ctx),
                  db: Session = Depends(get_db)):
    store.get_owned_folder(db, ctx.user_id, folder_id)
    query = (db.query(m.MailMessage)
             .filter(m.MailMessage.folder_id == folder_id,
                     m.MailMessage.user_id == ctx.user_id,
                     m.MailMessage.deleted_at.is_(None)))
    if unread_only:
        query = query.filter(m.MailMessage.is_read == 0)
    if flagged_only:
        query = query.filter(m.MailMessage.is_flagged == 1)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            m.MailMessage.subject.like(like), m.MailMessage.from_address.like(like),
            m.MailMessage.from_name.like(like), m.MailMessage.snippet.like(like)))
    if category_id:
        query = query.join(
            m.MailMessageCategory,
            m.MailMessageCategory.message_id == m.MailMessage.id).filter(
                m.MailMessageCategory.category_id == category_id)
    rows = (query.options(*_LIST_DEFER)
            .order_by(m.MailMessage.internal_date.desc(), m.MailMessage.id.desc())
            .offset((page - 1) * limit).limit(limit).all())
    cat_map = store.category_map_for(db, [r.id for r in rows])
    return [store.serialize_message_item(db, msg, categories=cat_map.get(msg.id, []))
            for msg in rows]


# ------------------------------------------------------------------ read
@router.get("/messages/{message_id}", response_model=MessageOut)
def get_message(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    settings = _settings_for(db, msg.account_id)
    behavior = settings.mark_read_behavior if settings else "on_selection"
    if not msg.is_read and behavior != "never":
        msg.is_read = 1
        store.refresh_folder_counts(db, msg.folder_id)
        db.commit()
        db.refresh(msg)
    return store.serialize_message_full(db, msg)


@router.get("/threads/{conversation_id}", response_model=List[MessageOut])
def get_thread(conversation_id: str, account_id: int = Query(...),
               ctx: AuthCtx = Depends(get_auth_ctx),
               db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    rows = (db.query(m.MailMessage)
            .filter(m.MailMessage.account_id == account_id,
                    m.MailMessage.user_id == ctx.user_id,
                    m.MailMessage.conversation_id == conversation_id,
                    m.MailMessage.deleted_at.is_(None))
            .order_by(m.MailMessage.internal_date.asc()).all())
    return [store.serialize_message_full(db, msg) for msg in rows]


@router.get("/search", response_model=List[MessageListItem])
def search(q: str = Query(..., min_length=1),
           account_id: Optional[int] = Query(None),
           folder_id: Optional[int] = Query(None),
           limit: int = Query(50, ge=1, le=200),
           ctx: AuthCtx = Depends(get_auth_ctx),
           db: Session = Depends(get_db)):
    like = f"%{q}%"
    query = (db.query(m.MailMessage)
             .filter(m.MailMessage.user_id == ctx.user_id,
                     m.MailMessage.deleted_at.is_(None),
                     or_(m.MailMessage.subject.like(like),
                         m.MailMessage.from_address.like(like),
                         m.MailMessage.from_name.like(like),
                         m.MailMessage.snippet.like(like),
                         m.MailMessage.body_text.like(like))))
    if account_id:
        query = query.filter(m.MailMessage.account_id == account_id)
    if folder_id:
        query = query.filter(m.MailMessage.folder_id == folder_id)
    rows = (query.options(*_LIST_DEFER)
            .order_by(m.MailMessage.internal_date.desc()).limit(limit).all())
    cat_map = store.category_map_for(db, [r.id for r in rows])
    return [store.serialize_message_item(db, msg, categories=cat_map.get(msg.id, []))
            for msg in rows]


# ------------------------------------------------------------------ actions
def _set_read(db, msg, value):
    if msg.is_read != value:
        msg.is_read = value
        store.refresh_folder_counts(db, msg.folder_id)


@router.post("/messages/{message_id}/read")
def mark_read(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
              db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    _set_read(db, msg, 1)
    db.commit()
    return {"ok": True}


@router.post("/messages/{message_id}/unread")
def mark_unread(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    _set_read(db, msg, 0)
    db.commit()
    return {"ok": True}


@router.post("/messages/{message_id}/flag")
def flag(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
         db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    msg.is_flagged = 1
    db.commit()
    return {"ok": True}


@router.post("/messages/{message_id}/unflag")
def unflag(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
           db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    msg.is_flagged = 0
    db.commit()
    return {"ok": True}


@router.post("/messages/{message_id}/move")
def move(message_id: int, body: MoveRequest,
         ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    target = store.get_owned_folder(db, ctx.user_id, body.folder_id)
    store.move_message(db, msg, target.id)
    db.commit()
    return {"ok": True}


@router.post("/messages/{message_id}/trash")
def trash(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
          db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    tr = store.get_folder_by_role(db, msg.account_id, "trash")
    if not tr:
        raise HTTPException(400, "No trash folder")
    store.move_message(db, msg, tr.id)
    db.commit()
    return {"ok": True}


@router.post("/messages/{message_id}/restore")
def restore(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
            db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    inbox = store.get_folder_by_role(db, msg.account_id, "inbox")
    if not inbox:
        raise HTTPException(400, "No inbox folder")
    store.move_message(db, msg, inbox.id)
    db.commit()
    return {"ok": True}


@router.delete("/messages/{message_id}")
def hard_delete(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    fid = msg.folder_id
    msg.deleted_at = datetime.utcnow()
    store.refresh_folder_counts(db, fid)
    db.commit()
    return {"ok": True}


def _junk(db, ctx, msg, is_junk: bool):
    role = "junk" if is_junk else "inbox"
    target = store.get_folder_by_role(db, msg.account_id, role)
    if target:
        store.move_message(db, msg, target.id)
    addr = (msg.from_address or "").lower()
    if addr:
        list_type = "blocked" if is_junk else "safe"
        exists = (db.query(m.MailSenderList).filter_by(
            account_id=msg.account_id, list_type=list_type,
            address_or_domain=addr).first())
        if not exists:
            db.add(m.MailSenderList(account_id=msg.account_id, user_id=ctx.user_id,
                                    list_type=list_type, address_or_domain=addr))


@router.post("/messages/{message_id}/junk")
def mark_junk(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
              db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    _junk(db, ctx, msg, True)
    db.commit()
    return {"ok": True}


@router.post("/messages/{message_id}/not-junk")
def mark_not_junk(message_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                  db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    _junk(db, ctx, msg, False)
    db.commit()
    return {"ok": True}


@router.post("/messages/{message_id}/categories")
def set_categories(message_id: int, body: CategoriesRequest,
                   ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    msg = store.get_owned_message(db, ctx.user_id, message_id)
    db.query(m.MailMessageCategory).filter(
        m.MailMessageCategory.message_id == msg.id).delete()
    valid = {c.id for c in db.query(m.MailCategory).filter(
        m.MailCategory.account_id == msg.account_id,
        m.MailCategory.id.in_(body.category_ids or [])).all()}
    for cid in valid:
        db.add(m.MailMessageCategory(message_id=msg.id, category_id=cid))
    db.commit()
    return {"ok": True, "categories": list(valid)}


@router.post("/messages/bulk")
def bulk(body: BulkRequest, ctx: AuthCtx = Depends(get_auth_ctx),
         db: Session = Depends(get_db)):
    action = body.action.lower()
    done = 0
    for mid in body.ids:
        msg = (db.query(m.MailMessage)
               .filter(m.MailMessage.id == mid,
                       m.MailMessage.user_id == ctx.user_id,
                       m.MailMessage.deleted_at.is_(None)).first())
        if not msg:
            continue
        if action == "read":
            _set_read(db, msg, 1)
        elif action == "unread":
            _set_read(db, msg, 0)
        elif action == "flag":
            msg.is_flagged = 1
        elif action == "unflag":
            msg.is_flagged = 0
        elif action == "move" and body.folder_id:
            tgt = store.get_owned_folder(db, ctx.user_id, body.folder_id)
            store.move_message(db, msg, tgt.id)
        elif action == "trash":
            tr = store.get_folder_by_role(db, msg.account_id, "trash")
            if tr:
                store.move_message(db, msg, tr.id)
        elif action == "junk":
            _junk(db, ctx, msg, True)
        elif action == "delete":
            fid = msg.folder_id
            msg.deleted_at = datetime.utcnow()
            store.refresh_folder_counts(db, fid)
        elif action == "categorize" and body.category_ids:
            for cid in body.category_ids:
                if not db.query(m.MailMessageCategory).filter_by(
                        message_id=msg.id, category_id=cid).first():
                    db.add(m.MailMessageCategory(message_id=msg.id, category_id=cid))
        done += 1
    db.commit()
    return {"ok": True, "affected": done}
