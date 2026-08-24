"""Apply junk/safe sender lists and user rules to a newly-arrived message.

Runs inside the sync transaction (flushes only; the caller commits). Ordered
by rule priority; a matching rule with ``stop_processing`` ends evaluation.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.mail_layer import models as m, store

logger = logging.getLogger("app_logger")


def _addr_domain(addr: Optional[str]) -> str:
    return (addr or "").split("@")[-1].lower()


def _field_value(msg: m.MailMessage, field: str) -> str:
    if field == "from":
        return f"{msg.from_name or ''} {msg.from_address or ''}".lower()
    if field == "to":
        return " ".join(a.get("address", "") for a in (msg.to_json or [])).lower()
    if field == "subject":
        return (msg.subject or "").lower()
    if field == "body":
        return (msg.body_text or "").lower()
    if field == "has_attachment":
        return "1" if msg.has_attachments else "0"
    return ""


def _match(msg: m.MailMessage, cond: dict) -> bool:
    field = (cond.get("field") or "").lower()
    op = (cond.get("op") or "contains").lower()
    value = str(cond.get("value", "")).lower()
    hay = _field_value(msg, field)
    if field == "has_attachment":
        return (hay == "1") == (value in ("1", "true", "yes"))
    if op == "contains":
        return value in hay
    if op == "not_contains":
        return value not in hay
    if op == "equals":
        return hay.strip() == value
    if op == "starts_with":
        return hay.strip().startswith(value)
    if op == "ends_with":
        return hay.strip().endswith(value)
    return False


def _move(db: Session, msg: m.MailMessage, target: m.MailFolder) -> None:
    store.move_message(db, msg, target.id)


def _apply_junk_lists(db: Session, account: m.MailAccount, msg: m.MailMessage) -> bool:
    """Returns True if the message was handled by a sender list (blocked→junk)."""
    addr = (msg.from_address or "").lower()
    if not addr:
        return False
    domain = _addr_domain(addr)
    entries = (db.query(m.MailSenderList)
               .filter(m.MailSenderList.account_id == account.id).all())
    blocked = {e.address_or_domain.lower() for e in entries if e.list_type == "blocked"}
    safe = {e.address_or_domain.lower() for e in entries if e.list_type == "safe"}
    if addr in safe or domain in safe:
        return True  # safe → leave in inbox, skip further junk handling
    if addr in blocked or domain in blocked:
        junk = store.get_folder_by_role(db, account.id, "junk")
        if junk:
            _move(db, msg, junk)
        return True
    return False


def apply_rules(db: Session, account: m.MailAccount, msg: m.MailMessage) -> None:
    try:
        if _apply_junk_lists(db, account, msg):
            return
        rules = (db.query(m.MailRule)
                 .filter(m.MailRule.account_id == account.id,
                         m.MailRule.is_enabled == 1)
                 .order_by(m.MailRule.priority.asc(), m.MailRule.id.asc())
                 .all())
        for rule in rules:
            conds = rule.conditions_json or []
            if not conds or not all(_match(msg, c) for c in conds):
                continue
            for action in (rule.actions_json or []):
                _apply_action(db, account, msg, action)
            if rule.stop_processing:
                break
    except Exception:
        logger.exception("rule evaluation failed for msg %s", getattr(msg, "id", "?"))


def run_rule_on_messages(db: Session, account: m.MailAccount, rule: m.MailRule,
                         folder_id: Optional[int] = None, limit: int = 1000) -> int:
    """Apply a single rule to existing messages (default: the inbox). Used by
    POST /mail/rules/{id}/run. Caller commits."""
    target_folder = folder_id
    if target_folder is None:
        inbox = store.get_folder_by_role(db, account.id, "inbox")
        target_folder = inbox.id if inbox else None
    if target_folder is None:
        return 0
    msgs = (db.query(m.MailMessage)
            .filter(m.MailMessage.account_id == account.id,
                    m.MailMessage.folder_id == target_folder,
                    m.MailMessage.deleted_at.is_(None))
            .limit(limit).all())
    conds = rule.conditions_json or []
    affected = 0
    for msg in msgs:
        if not conds or not all(_match(msg, c) for c in conds):
            continue
        for action in (rule.actions_json or []):
            _apply_action(db, account, msg, action)
        affected += 1
    return affected


def _apply_action(db: Session, account: m.MailAccount, msg: m.MailMessage,
                  action: dict) -> None:
    atype = (action.get("type") or "").lower()
    value = action.get("value")
    if atype == "mark_read":
        msg.is_read = 1
    elif atype == "flag":
        msg.is_flagged = 1
    elif atype == "categorize" and value:
        exists = (db.query(m.MailMessageCategory)
                  .filter_by(message_id=msg.id, category_id=int(value)).first())
        if not exists:
            db.add(m.MailMessageCategory(message_id=msg.id, category_id=int(value)))
    elif atype == "move_to" and value:
        target = None
        # value may be a folder role or a folder id
        if isinstance(value, str) and not str(value).isdigit():
            target = store.get_folder_by_role(db, account.id, value)
        else:
            target = (db.query(m.MailFolder)
                      .filter(m.MailFolder.id == int(value),
                              m.MailFolder.account_id == account.id).first())
        if target:
            _move(db, msg, target)
    elif atype == "delete":
        trash = store.get_folder_by_role(db, account.id, "trash")
        if trash:
            _move(db, msg, trash)
    elif atype == "forward":
        # Auto-forward needs an outbound send; deferred in v1.
        logger.info("rule forward action skipped (deferred) for msg %s", msg.id)
    db.flush()
