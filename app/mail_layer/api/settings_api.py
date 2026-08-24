"""Settings, signatures, rules, junk lists and categories."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.mail_layer import models as m, store
from app.mail_layer.schemas import (
    AutoReplyOut, AutoReplyUpdate, CategoryIn, CategoryOut, ForwardingUpdate,
    RuleIn, RuleOut, SenderIn, SenderOut, SettingsOut, SettingsUpdate,
    SignatureIn, SignatureOut,
)

router = APIRouter()


def _settings(db: Session, ctx: AuthCtx, account_id: int) -> m.MailSettings:
    acct = store.get_owned_account(db, ctx.user_id, account_id)
    return store.ensure_settings(db, acct)


# ------------------------------------------------------------------ settings
def _serialize_settings(s: m.MailSettings) -> dict:
    return {
        "focused_inbox": bool(s.focused_inbox),
        "conversation_view": bool(s.conversation_view),
        "reading_pane": s.reading_pane, "preview_lines": s.preview_lines,
        "mark_read_behavior": s.mark_read_behavior,
        "mark_read_delay_seconds": s.mark_read_delay_seconds,
        "external_images": s.external_images, "compose_format": s.compose_format,
        "default_font": s.default_font, "default_font_size": s.default_font_size,
        "undo_send_seconds": s.undo_send_seconds,
        "request_read_receipt": bool(s.request_read_receipt),
        "request_delivery_receipt": bool(s.request_delivery_receipt),
        "read_receipt_response": s.read_receipt_response,
        "empty_trash_on_exit": bool(s.empty_trash_on_exit),
        "prefs_json": s.prefs_json,
    }


@router.get("/accounts/{account_id}/settings", response_model=SettingsOut)
def get_settings(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                 db: Session = Depends(get_db)):
    return _serialize_settings(_settings(db, ctx, account_id))


@router.put("/accounts/{account_id}/settings", response_model=SettingsOut)
def update_settings(account_id: int, body: SettingsUpdate,
                    ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    s = _settings(db, ctx, account_id)
    data = body.model_dump(exclude_unset=True)
    bool_fields = {"focused_inbox", "conversation_view", "request_read_receipt",
                   "request_delivery_receipt", "empty_trash_on_exit"}
    for k, v in data.items():
        if v is None:
            continue
        setattr(s, k, (1 if v else 0) if k in bool_fields else v)
    db.commit()
    db.refresh(s)
    return _serialize_settings(s)


# ------------------------------------------------------------------ auto-reply
@router.get("/accounts/{account_id}/auto-reply", response_model=AutoReplyOut)
def get_auto_reply(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    s = _settings(db, ctx, account_id)
    return AutoReplyOut(
        auto_reply_enabled=bool(s.auto_reply_enabled),
        auto_reply_subject=s.auto_reply_subject,
        auto_reply_message_html=s.auto_reply_message_html,
        auto_reply_start=s.auto_reply_start, auto_reply_end=s.auto_reply_end,
        auto_reply_internal_only=bool(s.auto_reply_internal_only))


@router.put("/accounts/{account_id}/auto-reply", response_model=AutoReplyOut)
def set_auto_reply(account_id: int, body: AutoReplyUpdate,
                   ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    s = _settings(db, ctx, account_id)
    s.auto_reply_enabled = 1 if body.auto_reply_enabled else 0
    s.auto_reply_subject = body.auto_reply_subject
    s.auto_reply_message_html = body.auto_reply_message_html
    s.auto_reply_start = body.auto_reply_start
    s.auto_reply_end = body.auto_reply_end
    s.auto_reply_internal_only = 1 if body.auto_reply_internal_only else 0
    db.commit()
    db.refresh(s)
    return get_auto_reply(account_id, ctx, db)


@router.put("/accounts/{account_id}/forwarding")
def set_forwarding(account_id: int, body: ForwardingUpdate,
                   ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    s = _settings(db, ctx, account_id)
    s.forwarding_enabled = 1 if body.forwarding_enabled else 0
    s.forwarding_address = body.forwarding_address
    s.forwarding_keep_copy = 1 if body.forwarding_keep_copy else 0
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ signatures
@router.get("/accounts/{account_id}/signatures", response_model=List[SignatureOut])
def list_signatures(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    rows = db.query(m.MailSignature).filter(
        m.MailSignature.account_id == account_id).all()
    return [SignatureOut(id=r.id, name=r.name, body_html=r.body_html,
                         is_default_new=bool(r.is_default_new),
                         is_default_reply=bool(r.is_default_reply)) for r in rows]


@router.post("/accounts/{account_id}/signatures", response_model=SignatureOut)
def create_signature(account_id: int, body: SignatureIn,
                     ctx: AuthCtx = Depends(get_auth_ctx),
                     db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    sig = m.MailSignature(
        account_id=account_id, user_id=ctx.user_id, name=body.name,
        body_html=body.body_html,
        is_default_new=1 if body.is_default_new else 0,
        is_default_reply=1 if body.is_default_reply else 0)
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return SignatureOut(id=sig.id, **body.model_dump())


@router.put("/signatures/{sig_id}", response_model=SignatureOut)
def update_signature(sig_id: int, body: SignatureIn,
                     ctx: AuthCtx = Depends(get_auth_ctx),
                     db: Session = Depends(get_db)):
    sig = db.query(m.MailSignature).filter(
        m.MailSignature.id == sig_id, m.MailSignature.user_id == ctx.user_id).first()
    if not sig:
        raise HTTPException(404, "Signature not found")
    sig.name = body.name
    sig.body_html = body.body_html
    sig.is_default_new = 1 if body.is_default_new else 0
    sig.is_default_reply = 1 if body.is_default_reply else 0
    db.commit()
    return SignatureOut(id=sig.id, **body.model_dump())


@router.delete("/signatures/{sig_id}")
def delete_signature(sig_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                     db: Session = Depends(get_db)):
    n = db.query(m.MailSignature).filter(
        m.MailSignature.id == sig_id, m.MailSignature.user_id == ctx.user_id).delete()
    db.commit()
    if not n:
        raise HTTPException(404, "Signature not found")
    return {"ok": True}


# ------------------------------------------------------------------ rules
def _rule_out(r: m.MailRule) -> RuleOut:
    return RuleOut(id=r.id, name=r.name, is_enabled=bool(r.is_enabled),
                   priority=r.priority, conditions_json=r.conditions_json,
                   actions_json=r.actions_json, stop_processing=bool(r.stop_processing))


@router.get("/accounts/{account_id}/rules", response_model=List[RuleOut])
def list_rules(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
               db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    rows = (db.query(m.MailRule).filter(m.MailRule.account_id == account_id)
            .order_by(m.MailRule.priority.asc(), m.MailRule.id.asc()).all())
    return [_rule_out(r) for r in rows]


@router.post("/accounts/{account_id}/rules", response_model=RuleOut)
def create_rule(account_id: int, body: RuleIn,
                ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    rule = m.MailRule(
        account_id=account_id, user_id=ctx.user_id, name=body.name,
        is_enabled=1 if body.is_enabled else 0, priority=body.priority,
        conditions_json=body.conditions_json, actions_json=body.actions_json,
        stop_processing=1 if body.stop_processing else 0)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.put("/rules/{rule_id}", response_model=RuleOut)
def update_rule(rule_id: int, body: RuleIn, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    rule = db.query(m.MailRule).filter(
        m.MailRule.id == rule_id, m.MailRule.user_id == ctx.user_id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")
    rule.name = body.name
    rule.is_enabled = 1 if body.is_enabled else 0
    rule.priority = body.priority
    rule.conditions_json = body.conditions_json
    rule.actions_json = body.actions_json
    rule.stop_processing = 1 if body.stop_processing else 0
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    n = db.query(m.MailRule).filter(
        m.MailRule.id == rule_id, m.MailRule.user_id == ctx.user_id).delete()
    db.commit()
    if not n:
        raise HTTPException(404, "Rule not found")
    return {"ok": True}


@router.post("/rules/{rule_id}/run")
def run_rule(rule_id: int, folder_id: Optional[int] = Query(None),
             ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    from app.mail_layer.rules_engine import run_rule_on_messages
    rule = db.query(m.MailRule).filter(
        m.MailRule.id == rule_id, m.MailRule.user_id == ctx.user_id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")
    acct = store.get_owned_account(db, ctx.user_id, rule.account_id)
    affected = run_rule_on_messages(db, acct, rule, folder_id=folder_id)
    db.commit()
    return {"ok": True, "affected": affected}


# ------------------------------------------------------------------ senders
@router.get("/accounts/{account_id}/senders", response_model=List[SenderOut])
def list_senders(account_id: int, list_type: Optional[str] = Query(None),
                 ctx: AuthCtx = Depends(get_auth_ctx),
                 db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    q = db.query(m.MailSenderList).filter(m.MailSenderList.account_id == account_id)
    if list_type:
        q = q.filter(m.MailSenderList.list_type == list_type)
    return [SenderOut(id=r.id, list_type=r.list_type,
                      address_or_domain=r.address_or_domain) for r in q.all()]


@router.post("/accounts/{account_id}/senders", response_model=SenderOut)
def add_sender(account_id: int, body: SenderIn,
               ctx: AuthCtx = Depends(get_auth_ctx),
               db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    if body.list_type not in ("blocked", "safe"):
        raise HTTPException(400, "list_type must be 'blocked' or 'safe'")
    existing = db.query(m.MailSenderList).filter_by(
        account_id=account_id, list_type=body.list_type,
        address_or_domain=body.address_or_domain.lower()).first()
    if existing:
        return SenderOut(id=existing.id, list_type=existing.list_type,
                         address_or_domain=existing.address_or_domain)
    row = m.MailSenderList(account_id=account_id, user_id=ctx.user_id,
                           list_type=body.list_type,
                           address_or_domain=body.address_or_domain.lower())
    db.add(row)
    db.commit()
    db.refresh(row)
    return SenderOut(id=row.id, list_type=row.list_type,
                     address_or_domain=row.address_or_domain)


@router.delete("/senders/{sender_id}")
def delete_sender(sender_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                  db: Session = Depends(get_db)):
    n = db.query(m.MailSenderList).filter(
        m.MailSenderList.id == sender_id,
        m.MailSenderList.user_id == ctx.user_id).delete()
    db.commit()
    if not n:
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ------------------------------------------------------------------ categories
@router.get("/accounts/{account_id}/categories", response_model=List[CategoryOut])
def list_categories(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    rows = db.query(m.MailCategory).filter(
        m.MailCategory.account_id == account_id).all()
    return [CategoryOut(id=r.id, name=r.name, color=r.color) for r in rows]


@router.post("/accounts/{account_id}/categories", response_model=CategoryOut)
def create_category(account_id: int, body: CategoryIn,
                    ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    cat = m.MailCategory(account_id=account_id, user_id=ctx.user_id,
                         name=body.name, color=body.color)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return CategoryOut(id=cat.id, name=cat.name, color=cat.color)


@router.put("/categories/{cat_id}", response_model=CategoryOut)
def update_category(cat_id: int, body: CategoryIn,
                    ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    cat = db.query(m.MailCategory).filter(
        m.MailCategory.id == cat_id, m.MailCategory.user_id == ctx.user_id).first()
    if not cat:
        raise HTTPException(404, "Category not found")
    cat.name = body.name
    cat.color = body.color
    db.commit()
    return CategoryOut(id=cat.id, name=cat.name, color=cat.color)


@router.delete("/categories/{cat_id}")
def delete_category(cat_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    n = db.query(m.MailCategory).filter(
        m.MailCategory.id == cat_id, m.MailCategory.user_id == ctx.user_id).delete()
    db.commit()
    if not n:
        raise HTTPException(404, "Category not found")
    return {"ok": True}
