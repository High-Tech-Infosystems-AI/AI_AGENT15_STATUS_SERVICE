"""Fire-and-forget helpers so the API can kick a sync without blocking the
request. Each runs in a daemon thread with its own DB session."""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("app_logger")


def trigger_background_sync(account_id: int) -> None:
    def _run():
        from app.database_Layer.db_config import SessionLocal
        from app.mail_layer import imap_sync, models as m
        db = SessionLocal()
        try:
            acct = db.query(m.MailAccount).filter(m.MailAccount.id == account_id).first()
            if acct and acct.sync_enabled and acct.deleted_at is None:
                imap_sync.sync_account(db, acct)
        except Exception:
            logger.exception("background sync for account %s failed", account_id)
        finally:
            db.close()

    threading.Thread(target=_run, name=f"mail-sync-{account_id}", daemon=True).start()
