"""Data-access helpers for the mail layer. All reads/writes are scoped to a
single owner (``user_id``) — v1 has no cross-user access."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.mail_layer import models as m
from app.mail_layer.providers import SYSTEM_FOLDERS

logger = logging.getLogger("app_logger")


# ------------------------------------------------------------------ ownership
def get_owned_account(db: Session, user_id: int, account_id: int) -> m.MailAccount:
    acct = (db.query(m.MailAccount)
            .filter(m.MailAccount.id == account_id,
                    m.MailAccount.user_id == user_id,
                    m.MailAccount.deleted_at.is_(None))
            .first())
    if not acct:
        raise HTTPException(404, "Mailbox not found")
    return acct


def get_default_account(db: Session, user_id: int) -> Optional[m.MailAccount]:
    return (db.query(m.MailAccount)
            .filter(m.MailAccount.user_id == user_id,
                    m.MailAccount.deleted_at.is_(None))
            .order_by(m.MailAccount.is_default.desc(), m.MailAccount.id.asc())
            .first())


def get_owned_folder(db: Session, user_id: int, folder_id: int) -> m.MailFolder:
    fold = (db.query(m.MailFolder)
            .filter(m.MailFolder.id == folder_id,
                    m.MailFolder.user_id == user_id,
                    m.MailFolder.deleted_at.is_(None))
            .first())
    if not fold:
        raise HTTPException(404, "Folder not found")
    return fold


def get_folder_by_role(db: Session, account_id: int, role: str) -> Optional[m.MailFolder]:
    return (db.query(m.MailFolder)
            .filter(m.MailFolder.account_id == account_id,
                    m.MailFolder.role == role,
                    m.MailFolder.deleted_at.is_(None))
            .first())


def get_owned_message(db: Session, user_id: int, message_id: int) -> m.MailMessage:
    msg = (db.query(m.MailMessage)
           .filter(m.MailMessage.id == message_id,
                   m.MailMessage.user_id == user_id,
                   m.MailMessage.deleted_at.is_(None))
           .first())
    if not msg:
        raise HTTPException(404, "Message not found")
    return msg


# ------------------------------------------------------------------ seeding
def seed_system_folders(db: Session, account: m.MailAccount) -> None:
    """Create the Outlook system folders for a new account (idempotent)."""
    for role, name, order in SYSTEM_FOLDERS:
        exists = (db.query(m.MailFolder)
                  .filter(m.MailFolder.account_id == account.id,
                          m.MailFolder.role == role,
                          m.MailFolder.is_system == 1)
                  .first())
        if exists:
            continue
        db.add(m.MailFolder(
            account_id=account.id, user_id=account.user_id,
            organization_id=account.organization_id,
            name=name, role=role, sort_order=order, is_system=1,
        ))
    db.flush()


def ensure_settings(db: Session, account: m.MailAccount) -> m.MailSettings:
    s = (db.query(m.MailSettings)
         .filter(m.MailSettings.account_id == account.id).first())
    if s:
        return s
    s = m.MailSettings(account_id=account.id, user_id=account.user_id,
                       organization_id=account.organization_id)
    db.add(s)
    db.flush()
    return s


# ------------------------------------------------------------------ counts
def refresh_folder_counts(db: Session, folder_id: int) -> None:
    total = (db.query(func.count(m.MailMessage.id))
             .filter(m.MailMessage.folder_id == folder_id,
                     m.MailMessage.deleted_at.is_(None)).scalar() or 0)
    unread = (db.query(func.count(m.MailMessage.id))
              .filter(m.MailMessage.folder_id == folder_id,
                      m.MailMessage.deleted_at.is_(None),
                      m.MailMessage.is_read == 0).scalar() or 0)
    db.query(m.MailFolder).filter(m.MailFolder.id == folder_id).update(
        {"total_count": total, "unread_count": unread})


# ------------------------------------------------------------------ move
def move_message(db: Session, msg: m.MailMessage, target_folder_id: int) -> None:
    """Move a message locally. Clears imap_uid so the per-(account,folder,uid)
    unique key can't clash with an existing message in the target folder, and
    so a later incremental sync doesn't treat it as still-in-place."""
    old_folder = msg.folder_id
    if old_folder == target_folder_id:
        return
    msg.folder_id = target_folder_id
    msg.imap_uid = None
    db.flush()
    refresh_folder_counts(db, old_folder)
    refresh_folder_counts(db, target_folder_id)


# ------------------------------------------------------------------ categories
def category_ids_for(db: Session, message_id: int) -> List[int]:
    rows = (db.query(m.MailMessageCategory.category_id)
            .filter(m.MailMessageCategory.message_id == message_id).all())
    return [r[0] for r in rows]


# ------------------------------------------------------------------ serializers
def serialize_account(a: m.MailAccount) -> dict:
    return {
        "id": a.id, "display_name": a.display_name, "email_address": a.email_address,
        "provider": a.provider, "smtp_host": a.smtp_host, "smtp_port": a.smtp_port,
        "smtp_security": a.smtp_security, "smtp_username": a.smtp_username,
        "imap_host": a.imap_host, "imap_port": a.imap_port,
        "imap_security": a.imap_security, "imap_username": a.imap_username,
        "use_same_credentials": bool(a.use_same_credentials),
        "password_set": bool(a.smtp_password_enc),
        "sync_enabled": bool(a.sync_enabled),
        "sync_interval_seconds": a.sync_interval_seconds,
        "backfill_days": a.backfill_days, "use_idle": bool(a.use_idle),
        "status": a.status, "last_error": a.last_error,
        "last_synced_at": a.last_synced_at, "is_default": bool(a.is_default),
    }


def serialize_folder(f: m.MailFolder) -> dict:
    return {
        "id": f.id, "name": f.name, "role": f.role,
        "parent_folder_id": f.parent_folder_id,
        "unread_count": f.unread_count, "total_count": f.total_count,
        "sort_order": f.sort_order, "is_system": bool(f.is_system),
    }


def serialize_message_item(db: Session, msg: m.MailMessage) -> dict:
    return {
        "id": msg.id, "folder_id": msg.folder_id,
        "conversation_id": msg.conversation_id, "direction": msg.direction,
        "from_name": msg.from_name, "from_address": msg.from_address,
        "to": msg.to_json or [], "subject": msg.subject, "snippet": msg.snippet,
        "has_attachments": bool(msg.has_attachments), "is_read": bool(msg.is_read),
        "is_flagged": bool(msg.is_flagged), "is_draft": bool(msg.is_draft),
        "importance": msg.importance, "send_status": msg.send_status,
        "internal_date": msg.internal_date or msg.sent_at or msg.created_at,
        "categories": category_ids_for(db, msg.id),
    }


def serialize_attachment(att: m.MailAttachment) -> dict:
    return {
        "id": att.id, "file_name": att.file_name, "mime_type": att.mime_type,
        "size_bytes": att.size_bytes, "is_inline": bool(att.is_inline),
        "content_id": att.content_id, "fetched": bool(att.fetched),
    }


def serialize_message_full(db: Session, msg: m.MailMessage) -> dict:
    base = serialize_message_item(db, msg)
    base.update({
        "cc": msg.cc_json or [], "bcc": msg.bcc_json or [],
        "reply_to_address": msg.reply_to_address,
        "body_text": msg.body_text, "body_html": msg.body_html,
        "in_reply_to": msg.in_reply_to, "message_id_hdr": msg.message_id_hdr,
        "sent_at": msg.sent_at,
        "attachments": [serialize_attachment(a) for a in msg.attachments],
    })
    return base
