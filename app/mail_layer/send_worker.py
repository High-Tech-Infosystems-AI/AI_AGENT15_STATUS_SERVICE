"""SMTP send worker — drains the Outbox.

Picks up messages with send_status='queued' whose undo-send window has elapsed
(or whose scheduled_at has arrived), sends them via the account's SMTP, files
the copy in Sent (and best-effort IMAP APPEND), and retries with backoff.

Started by start.sh:  python -m app.mail_layer.send_worker
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from app.core import log  # noqa: F401
from app.database_Layer.db_config import SessionLocal
from app.mail_layer import models as m, smtp_send, store
from app.mail_layer.crypto import decrypt

logger = logging.getLogger("app_logger")

POLL_SECONDS = int(os.getenv("MAIL_SEND_POLL_SECONDS", "5"))
MAX_RETRIES = int(os.getenv("MAIL_MAX_SEND_RETRIES", "5"))


def _recipients(msg: m.MailMessage):
    out = []
    for bucket in (msg.to_json, msg.cc_json, msg.bcc_json):
        for a in (bucket or []):
            addr = a.get("address")
            if addr:
                out.append(addr)
    return out


def _undo_elapsed(db, msg: m.MailMessage) -> bool:
    """True once the message may actually leave — past the per-account
    undo-send window, or past its scheduled time."""
    now = datetime.utcnow()
    if msg.scheduled_at:
        return now >= msg.scheduled_at
    s = db.query(m.MailSettings).filter(
        m.MailSettings.account_id == msg.account_id).first()
    undo = (s.undo_send_seconds if s else 10) or 0
    base = msg.updated_at or msg.created_at or now
    return (now - base) >= timedelta(seconds=undo)


def _append_to_sent(account: m.MailAccount, sent_folder: m.MailFolder, raw: bytes):
    if not sent_folder or not sent_folder.imap_path:
        return
    try:
        from app.mail_layer import imap_sync
        server = imap_sync.connect(account)
        try:
            server.append(sent_folder.imap_path, raw, flags=[b"\\Seen"])
        finally:
            server.logout()
    except Exception as exc:
        logger.info("IMAP APPEND to Sent skipped (%s)", exc)


def _send_one(db, msg: m.MailMessage) -> None:
    account = db.query(m.MailAccount).filter(
        m.MailAccount.id == msg.account_id,
        m.MailAccount.deleted_at.is_(None)).first()
    if not account:
        msg.send_status = "failed"
        msg.send_error = "mailbox no longer connected"
        db.commit()
        return

    recipients = _recipients(msg)
    if not recipients:
        msg.send_status = "failed"
        msg.send_error = "no recipients"
        db.commit()
        return

    msg.send_status = "sending"
    db.commit()

    attachments = []
    if msg.has_attachments:
        from app.mail_layer import s3_mail_service
        for att in msg.attachments:
            data = s3_mail_service.download(att.s3_key) if att.s3_key else None
            attachments.append({"file_name": att.file_name,
                                "mime_type": att.mime_type, "data": data or b""})

    to = [a.get("address") for a in (msg.to_json or []) if a.get("address")]
    cc = [a.get("address") for a in (msg.cc_json or []) if a.get("address")]
    bcc = [a.get("address") for a in (msg.bcc_json or []) if a.get("address")]

    mime = smtp_send.build_message(
        from_addr=account.email_address, from_name=account.display_name,
        to=to, cc=cc, bcc=bcc, subject=msg.subject or "",
        body_html=msg.body_html, body_text=msg.body_text,
        in_reply_to=msg.in_reply_to, references=msg.references_hdr,
        attachments=attachments)

    password = decrypt(account.smtp_password_enc)
    ok, detail = smtp_send.send(
        host=account.smtp_host, port=account.smtp_port,
        security=account.smtp_security, username=account.smtp_username,
        password=password, msg=mime, from_addr=account.email_address,
        recipients=recipients)

    if ok:
        sent_folder = store.get_folder_by_role(db, account.id, "sent")
        msg.send_status = "sent"
        msg.send_error = None
        msg.sent_at = datetime.utcnow()
        msg.message_id_hdr = mime["Message-ID"]
        msg.is_read = 1
        if sent_folder:
            store.move_message(db, msg, sent_folder.id)
        db.commit()
        try:
            _append_to_sent(account, sent_folder, mime.as_bytes())
        except Exception:
            pass
        logger.info("Sent message %s for account %s", msg.id, account.id)
    else:
        msg.send_attempts = (msg.send_attempts or 0) + 1
        msg.send_error = detail[:2000]
        if msg.send_attempts >= MAX_RETRIES:
            msg.send_status = "failed"
        else:
            msg.send_status = "queued"  # retry next pass
        db.commit()
        logger.warning("Send failed for message %s (attempt %s): %s",
                       msg.id, msg.send_attempts, detail)


def run() -> None:
    logger.info("Mail send worker started (poll=%ss, max_retries=%s)",
                POLL_SECONDS, MAX_RETRIES)
    while True:
        try:
            db = SessionLocal()
            try:
                now = datetime.utcnow()
                candidates = (db.query(m.MailMessage)
                              .filter(m.MailMessage.send_status == "queued",
                                      m.MailMessage.deleted_at.is_(None))
                              .order_by(m.MailMessage.created_at.asc())
                              .limit(20).all())
                for msg in candidates:
                    if msg.scheduled_at and msg.scheduled_at > now:
                        continue
                    if not _undo_elapsed(db, msg):
                        continue
                    _send_one(db, msg)
            finally:
                db.close()
        except Exception:
            logger.exception("mail send loop error")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
