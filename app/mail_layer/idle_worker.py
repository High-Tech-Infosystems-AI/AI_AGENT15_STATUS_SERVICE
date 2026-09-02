"""IMAP IDLE worker — real-time receive.

For each connected account, holds a persistent IMAP IDLE connection on INBOX.
When the server pushes a new-mail signal, runs an incremental sync and publishes
a ``mail:new`` event to the user (delivered to the browser by the mail
WebSocket). The polling sync_worker still runs as a safety net / for other
folders. Started by start.sh:  python -m app.mail_layer.idle_worker

Disable with MAIL_IDLE_ENABLED=0.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from app.core import log  # noqa: F401  (configures logging)
from app.database_Layer.db_config import SessionLocal
from app.mail_layer import imap_sync, models as m, realtime

logger = logging.getLogger("app_logger")

RECONCILE_SECONDS = int(os.getenv("MAIL_IDLE_RECONCILE_SECONDS", "60"))
# Gmail drops an idle connection ~29 min; refresh before that.
IDLE_REFRESH_SECONDS = min(int(os.getenv("MAIL_IDLE_REFRESH_SECONDS", str(28 * 60))), 1740)
MAX_IDLE_ACCOUNTS = int(os.getenv("MAIL_IDLE_MAX_ACCOUNTS", "100"))

_threads: dict[int, threading.Thread] = {}
_stops: dict[int, threading.Event] = {}


def _inbox_count(db, account_id: int) -> int:
    inbox = (db.query(m.MailFolder)
             .filter(m.MailFolder.account_id == account_id,
                     m.MailFolder.role == "inbox",
                     m.MailFolder.deleted_at.is_(None)).first())
    if not inbox:
        return 0
    return (db.query(m.MailMessage)
            .filter(m.MailMessage.folder_id == inbox.id,
                    m.MailMessage.deleted_at.is_(None)).count())


def _sync_and_notify(account_id: int) -> None:
    db = SessionLocal()
    try:
        acct = db.query(m.MailAccount).filter(m.MailAccount.id == account_id).first()
        if not acct:
            return
        before = _inbox_count(db, acct.id)
        imap_sync.sync_account(db, acct)
        after = _inbox_count(db, acct.id)
        realtime.publish_mail_event(acct.user_id, {
            "type": "mail:new",
            "account_id": acct.id,
            "new_count": max(0, after - before),
        })
    except Exception:
        logger.exception("idle sync/notify failed for account %s", account_id)
    finally:
        db.close()


def _has_new_mail(responses) -> bool:
    """imapclient returns e.g. [(1, b'EXISTS'), (1, b'RECENT')] on new mail."""
    for item in (responses or []):
        parts = item if isinstance(item, (list, tuple)) else [item]
        for part in parts:
            if isinstance(part, bytes) and part.upper() in (b"EXISTS", b"RECENT"):
                return True
    return False


def _idle_loop(account_id: int, stop: threading.Event) -> None:
    backoff = 5
    while not stop.is_set():
        server = None
        try:
            db = SessionLocal()
            acct = db.query(m.MailAccount).filter(m.MailAccount.id == account_id).first()
            db.close()
            if not acct or acct.deleted_at is not None or not acct.sync_enabled:
                return

            server = imap_sync.connect(acct)
            server.select_folder("INBOX")
            # Catch up on anything that arrived while we were disconnected.
            _sync_and_notify(account_id)
            backoff = 5

            while not stop.is_set():
                server.idle()
                try:
                    responses = server.idle_check(timeout=IDLE_REFRESH_SECONDS)
                finally:
                    server.idle_done()
                if stop.is_set():
                    break
                if _has_new_mail(responses):
                    _sync_and_notify(account_id)
                # else: idle refresh cycle — just re-enter idle.
        except Exception as exc:
            logger.info("IDLE account %s error: %s; reconnecting in %ss",
                        account_id, exc, backoff)
            if stop.wait(backoff):
                break
            backoff = min(backoff * 2, 300)
        finally:
            if server is not None:
                try:
                    server.logout()
                except Exception:
                    pass


def _reconcile() -> None:
    db = SessionLocal()
    try:
        accts = (db.query(m.MailAccount)
                 .filter(m.MailAccount.sync_enabled == 1,
                         m.MailAccount.deleted_at.is_(None),
                         m.MailAccount.status == "connected")
                 .order_by(m.MailAccount.id.asc())
                 .limit(MAX_IDLE_ACCOUNTS).all())
        want = {a.id for a in accts}
    finally:
        db.close()

    for aid in want:
        t = _threads.get(aid)
        if t and t.is_alive():
            continue
        ev = threading.Event()
        _stops[aid] = ev
        t = threading.Thread(target=_idle_loop, args=(aid, ev),
                             daemon=True, name=f"mail-idle-{aid}")
        _threads[aid] = t
        t.start()
        logger.info("IDLE started for account %s", aid)

    for aid in list(_threads.keys()):
        if aid not in want:
            _stops[aid].set()
            _threads.pop(aid, None)
            _stops.pop(aid, None)
            logger.info("IDLE stopped for account %s", aid)


def run() -> None:
    if os.getenv("MAIL_IDLE_ENABLED", "1") not in ("1", "true", "yes"):
        logger.info("MAIL_IDLE_ENABLED off — idle worker idle")
        while True:
            time.sleep(3600)
    logger.info("Mail IDLE worker started (reconcile=%ss, refresh=%ss, max=%s)",
                RECONCILE_SECONDS, IDLE_REFRESH_SECONDS, MAX_IDLE_ACCOUNTS)
    while True:
        try:
            _reconcile()
        except Exception:
            logger.exception("idle reconcile loop error")
        time.sleep(RECONCILE_SECONDS)


if __name__ == "__main__":
    run()
