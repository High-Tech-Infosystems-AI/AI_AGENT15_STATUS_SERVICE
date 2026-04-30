"""AI Assistant bot user — synthetic user that owns the per-user AI DM thread.

Mirrors `chat_layer/status_bot.py`. Provisioned once at service startup;
becomes the `sender_id` of every AI reply and the peer of every user's
"AI Assistant" DM (rendered pinned at the top of the chat list).
"""
from __future__ import annotations

import logging
from threading import Lock
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

AI_BOT_USERNAME = "ai_assistant"
AI_BOT_DISPLAY_NAME = "HTI Chat"

_BOT_USER_ID: Optional[int] = None
_BOT_LOCK = Lock()


def ensure_ai_bot_user(db: Session) -> int:
    """Return the bot user_id, creating it if missing. Idempotent + cached.

    Also reconciles the display name on every cold start: if the bot row
    already exists with a different name (e.g. an older "AI Assistant"
    deployment), we update it to the current `AI_BOT_DISPLAY_NAME` so the
    chat sidebar shows the right brand without manual intervention.
    """
    global _BOT_USER_ID
    with _BOT_LOCK:
        if _BOT_USER_ID is not None:
            return _BOT_USER_ID

        from sqlalchemy import text
        from app.chat_layer._synthetic_user import provision_synthetic_user

        try:
            _BOT_USER_ID = provision_synthetic_user(
                db,
                username=AI_BOT_USERNAME,
                display_name=AI_BOT_DISPLAY_NAME,
                email="ai-assistant@chat.local",
            )
        except Exception:
            db.rollback()
            raise

        # Keep the display name in sync after a rebrand. Quick guarded UPDATE
        # — runs only when the live row's `name` column differs.
        try:
            row = db.execute(
                text("SELECT name FROM users WHERE id = :uid LIMIT 1"),
                {"uid": _BOT_USER_ID},
            ).first()
            if row and (row[0] or "") != AI_BOT_DISPLAY_NAME:
                db.execute(
                    text("UPDATE users SET name = :n WHERE id = :uid"),
                    {"n": AI_BOT_DISPLAY_NAME, "uid": _BOT_USER_ID},
                )
                db.commit()
                logger.info("AI bot display name updated to %r", AI_BOT_DISPLAY_NAME)
        except Exception as exc:
            logger.warning("AI bot name reconcile skipped: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass

        logger.info("HTI Chat bot user provisioned id=%s", _BOT_USER_ID)
        return _BOT_USER_ID


def get_ai_bot_user_id() -> Optional[int]:
    """Cheap read of the cached id. Returns None until ensure_ai_bot_user ran."""
    return _BOT_USER_ID
