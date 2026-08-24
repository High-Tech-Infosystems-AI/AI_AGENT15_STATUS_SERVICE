"""IMAP receive — folder discovery + incremental message sync.

Uses ``imapclient`` (added to pyproject). Imported lazily so the FastAPI app
still boots if the package is missing on a given host — only the sync worker
needs it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.mail_layer import models as m
from app.mail_layer import parsing, s3_mail_service, store
from app.mail_layer.crypto import decrypt

logger = logging.getLogger("app_logger")

# Cap messages pulled per folder per run so a first backfill of a huge mailbox
# is spread across several passes instead of one very long transaction.
MAX_PER_FOLDER_PER_RUN = 200

# SPECIAL-USE flag (bytes) -> our folder role.
_SPECIAL_USE = {
    b"\\Sent": "sent",
    b"\\Junk": "junk",
    b"\\Trash": "trash",
    b"\\Drafts": "drafts",
    b"\\Archive": "archive",
}
# Folders we never pull from the server (outbox is local; drafts stay local in v1).
_SKIP_ROLES = {"outbox"}


def _imapclient():
    from imapclient import IMAPClient  # lazy
    return IMAPClient


def _decrypt_imap_password(account: m.MailAccount) -> str:
    blob = account.smtp_password_enc if account.use_same_credentials else account.imap_password_enc
    return decrypt(blob)


def connect(account: m.MailAccount):
    IMAPClient = _imapclient()
    use_ssl = (account.imap_security or "ssl").lower() == "ssl"
    server = IMAPClient(account.imap_host, port=account.imap_port, ssl=use_ssl, timeout=30)
    if (account.imap_security or "").lower() == "starttls":
        server.starttls()
    username = account.imap_username or account.email_address
    server.login(username, _decrypt_imap_password(account))
    return server


def test_imap(host: str, port: int, security: str, username: str,
              password: str) -> Tuple[bool, str, Optional[int]]:
    """Return (ok, detail, folder_count). Never raises."""
    try:
        IMAPClient = _imapclient()
    except Exception as exc:
        return False, f"imap client unavailable: {exc}", None
    use_ssl = (security or "ssl").lower() == "ssl"
    try:
        server = IMAPClient(host, port=port, ssl=use_ssl, timeout=20)
        if (security or "").lower() == "starttls":
            server.starttls()
    except Exception as exc:
        return False, f"connect failed: {exc}", None
    try:
        server.login(username, password)
        folders = server.list_folders()
        return True, "authenticated", len(folders)
    except Exception as exc:
        return False, f"login failed: {exc}", None
    finally:
        try:
            server.logout()
        except Exception:
            pass


def _role_for(flags, name: str) -> Optional[str]:
    if (name or "").upper() == "INBOX":
        return "inbox"
    for f in (flags or []):
        fb = f if isinstance(f, bytes) else str(f).encode()
        if fb in _SPECIAL_USE:
            return _SPECIAL_USE[fb]
    return None


def _sync_folders(db: Session, account: m.MailAccount, server) -> None:
    """Map server mailboxes to our folder rows: set imap_path on system folders,
    create custom folders for anything unrecognised."""
    server_folders = server.list_folders()  # [(flags, delimiter, name)]
    seen_roles = set()
    for flags, _delim, name in server_folders:
        role = _role_for(flags, name)
        if role:
            fold = store.get_folder_by_role(db, account.id, role)
            if fold and fold.imap_path != name:
                fold.imap_path = name
            seen_roles.add(role)
        else:
            # custom folder — create if we don't have it yet
            exists = (db.query(m.MailFolder)
                      .filter(m.MailFolder.account_id == account.id,
                              m.MailFolder.imap_path == name).first())
            if not exists:
                db.add(m.MailFolder(
                    account_id=account.id, user_id=account.user_id,
                    organization_id=account.organization_id,
                    name=name.split("/")[-1], role="custom",
                    imap_path=name, is_system=0, sort_order=50))
    db.flush()


def _upsert_message(db: Session, account: m.MailAccount, folder: m.MailFolder,
                    uid: int, raw: bytes, internal_date, flags) -> Optional[m.MailMessage]:
    existing = (db.query(m.MailMessage)
                .filter(m.MailMessage.account_id == account.id,
                        m.MailMessage.folder_id == folder.id,
                        m.MailMessage.imap_uid == uid).first())
    if existing:
        return None

    parsed = parsing.parse_message(raw)
    is_seen = 0
    is_flagged = 0
    for fl in (flags or []):
        flb = fl if isinstance(fl, bytes) else str(fl).encode()
        if flb == b"\\Seen":
            is_seen = 1
        if flb == b"\\Flagged":
            is_flagged = 1
    direction = "out" if folder.role == "sent" else "in"

    msg = m.MailMessage(
        account_id=account.id, folder_id=folder.id, user_id=account.user_id,
        organization_id=account.organization_id, imap_uid=uid,
        message_id_hdr=parsed["message_id_hdr"] or None,
        conversation_id=parsed["conversation_id"],
        in_reply_to=parsed["in_reply_to"], references_hdr=parsed["references_hdr"],
        direction=direction, from_name=parsed["from_name"],
        from_address=parsed["from_address"], to_json=parsed["to_json"],
        cc_json=parsed["cc_json"], reply_to_address=parsed["reply_to_address"],
        subject=parsed["subject"], snippet=parsing.make_snippet(parsed["body_text"]),
        body_text=parsed["body_text"], body_html=parsed["body_html"],
        has_attachments=parsed["has_attachments"], size_bytes=parsed["size_bytes"],
        is_read=is_seen, is_flagged=is_flagged, importance=parsed["importance"],
        sent_at=parsed["sent_at"], internal_date=internal_date or parsed["sent_at"],
        received_at=internal_date,
    )
    db.add(msg)
    db.flush()

    for att in parsed["attachments"]:
        s3_key = None
        fetched = 0
        if att.get("data"):
            s3_key = s3_mail_service.upload(
                data=att["data"], mime_type=att["mime_type"],
                file_name=att["file_name"], user_id=account.user_id)
            fetched = 1 if s3_key else 0
        db.add(m.MailAttachment(
            message_id=msg.id, account_id=account.id, user_id=account.user_id,
            s3_key=s3_key, content_id=att.get("content_id"),
            file_name=att["file_name"][:255], mime_type=att["mime_type"][:120],
            size_bytes=att["size_bytes"], is_inline=att.get("is_inline", 0),
            fetched=fetched))
    return msg


def _sync_one_folder(db: Session, account: m.MailAccount, folder: m.MailFolder,
                     server) -> int:
    if not folder.imap_path:
        return 0
    try:
        info = server.select_folder(folder.imap_path, readonly=True)
    except Exception as exc:
        logger.warning("select %s failed: %s", folder.imap_path, exc)
        return 0

    uidvalidity = info.get(b"UIDVALIDITY")
    if uidvalidity is not None and folder.uidvalidity != uidvalidity:
        folder.uidvalidity = uidvalidity
        folder.last_uid = 0  # server renumbered — re-scan window

    if folder.last_uid and folder.last_uid > 0:
        criteria = ["UID", f"{folder.last_uid + 1}:*"]
    else:
        since = datetime.utcnow() - timedelta(days=account.backfill_days or 90)
        criteria = ["SINCE", since.date()] if account.backfill_days else ["ALL"]

    try:
        uids = server.search(criteria)
    except Exception as exc:
        logger.warning("search %s failed: %s", folder.imap_path, exc)
        return 0
    # search UID x:* always returns at least the last uid — drop already-seen
    uids = [u for u in uids if not folder.last_uid or u > folder.last_uid]
    if not uids:
        return 0
    uids = sorted(uids)[:MAX_PER_FOLDER_PER_RUN]

    fetched = server.fetch(uids, ["RFC822", "INTERNALDATE", "FLAGS"])
    new_count = 0
    max_uid = folder.last_uid or 0
    from app.mail_layer.rules_engine import apply_rules
    for uid in uids:
        data = fetched.get(uid) or {}
        raw = data.get(b"RFC822")
        if not raw:
            continue
        msg = _upsert_message(db, account, folder, uid,
                              raw, data.get(b"INTERNALDATE"), data.get(b"FLAGS"))
        if msg:
            new_count += 1
            if folder.role == "inbox":
                apply_rules(db, account, msg)
        max_uid = max(max_uid, uid)

    folder.last_uid = max_uid
    db.flush()
    store.refresh_folder_counts(db, folder.id)
    return new_count


def sync_account(db: Session, account: m.MailAccount) -> dict:
    """Full pass over one account. Commits at the end. Returns a summary."""
    summary = {"account_id": account.id, "new": 0, "folders": 0, "ok": True}
    try:
        server = connect(account)
    except Exception as exc:
        account.status = "error"
        account.last_error = f"IMAP connect/login failed: {exc}"
        db.commit()
        summary.update(ok=False, error=str(exc))
        return summary

    try:
        _sync_folders(db, account, server)
        folders = (db.query(m.MailFolder)
                   .filter(m.MailFolder.account_id == account.id,
                           m.MailFolder.deleted_at.is_(None),
                           m.MailFolder.role.notin_(_SKIP_ROLES))
                   .all())
        for folder in folders:
            if folder.role == "drafts" and not folder.imap_path:
                continue
            summary["new"] += _sync_one_folder(db, account, folder, server)
            summary["folders"] += 1
        account.status = "connected"
        account.last_error = None
        account.last_synced_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("sync_account %s failed", account.id)
        account.status = "error"
        account.last_error = str(exc)[:2000]
        db.commit()
        summary.update(ok=False, error=str(exc))
    finally:
        try:
            server.logout()
        except Exception:
            pass
    return summary
