"""IMAP sync worker — a supervised long-running process.

Loops over accounts that are due a sync (based on their per-account
``sync_interval_seconds``) and mirrors new mail. Started by start.sh:

    python -m app.mail_layer.sync_worker

Never takes the pod down — the Status API owns container liveness. Disable with
MAIL_SYNC_ENABLED=0.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from app.core import log  # noqa: F401  (configures logging)
from app.database_Layer.db_config import SessionLocal
from app.mail_layer import imap_sync, models as m

logger = logging.getLogger("app_logger")

POLL_SECONDS = int(os.getenv("MAIL_SYNC_POLL_SECONDS", "30"))
CONCURRENCY = int(os.getenv("MAIL_SYNC_CONCURRENCY", "4"))


def _due_accounts(db):
    now = datetime.utcnow()
    accounts = (db.query(m.MailAccount)
                .filter(m.MailAccount.sync_enabled == 1,
                        m.MailAccount.deleted_at.is_(None),
                        m.MailAccount.status != "disconnected")
                .all())
    due = []
    for a in accounts:
        interval = timedelta(seconds=max(60, a.sync_interval_seconds or 300))
        if a.last_synced_at is None or (now - a.last_synced_at) >= interval:
            due.append(a.id)
    return due


def _sync_one(account_id: int) -> None:
    db = SessionLocal()
    try:
        acct = db.query(m.MailAccount).filter(m.MailAccount.id == account_id).first()
        if acct:
            imap_sync.sync_account(db, acct)
    except Exception:
        logger.exception("sync worker: account %s failed", account_id)
    finally:
        db.close()


def run() -> None:
    if os.getenv("MAIL_SYNC_ENABLED", "1") not in ("1", "true", "yes"):
        logger.info("MAIL_SYNC_ENABLED is off — sync worker idle")
        while True:
            time.sleep(3600)

    logger.info("Mail sync worker started (poll=%ss, concurrency=%s)",
                POLL_SECONDS, CONCURRENCY)
    while True:
        try:
            db = SessionLocal()
            try:
                due = _due_accounts(db)
            finally:
                db.close()
            if due:
                logger.info("Mail sync: %s account(s) due", len(due))
                with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                    list(pool.map(_sync_one, due))
        except Exception:
            logger.exception("mail sync loop error")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
