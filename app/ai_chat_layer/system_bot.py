"""AI Assistant bot user — synthetic user that owns the per-user AI DM thread.

Mirrors `chat_layer/status_bot.py`. Provisioned once at service startup;
becomes the `sender_id` of every AI reply and the peer of every user's
"AI Assistant" DM (rendered pinned at the top of the chat list).
"""
from __future__ import annotations

import logging
from threading import Lock
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

AI_BOT_USERNAME = "ai_assistant"
AI_BOT_DISPLAY_NAME = "AI Assistant"

_BOT_USER_ID: Optional[int] = None
_BOT_LOCK = Lock()


def ensure_ai_bot_user(db: Session) -> int:
    """Return the bot user_id, creating it if missing. Idempotent + cached."""
    global _BOT_USER_ID
    with _BOT_LOCK:
        if _BOT_USER_ID is not None:
            return _BOT_USER_ID

        row = db.execute(
            text("SELECT id FROM users WHERE username = :u LIMIT 1"),
            {"u": AI_BOT_USERNAME},
        ).first()
        if row:
            _BOT_USER_ID = int(row[0])
            return _BOT_USER_ID

        try:
            db.execute(
                text("""
                    INSERT INTO users
                        (name, username, email, password, role_id,
                         enable, deleted_at, created_at)
                    VALUES
                        (:name, :uname, :email, :pwd, NULL,
                         0, NULL, NOW())
                """),
                {
                    "name": AI_BOT_DISPLAY_NAME,
                    "uname": AI_BOT_USERNAME,
                    "email": "ai-assistant@chat.local",
                    "pwd": "!disabled!",
                },
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("AI bot insert (full schema) failed: %s — minimal", exc)
            db.execute(
                text("""
                    INSERT INTO users (name, username, email, enable)
                    VALUES (:name, :uname, :email, 0)
                """),
                {
                    "name": AI_BOT_DISPLAY_NAME,
                    "uname": AI_BOT_USERNAME,
                    "email": "ai-assistant@chat.local",
                },
            )
            db.commit()

        row = db.execute(
            text("SELECT id FROM users WHERE username = :u LIMIT 1"),
            {"u": AI_BOT_USERNAME},
        ).first()
        if not row:
            raise RuntimeError("Failed to create AI Assistant user")
        _BOT_USER_ID = int(row[0])
        logger.info("AI Assistant user provisioned id=%s", _BOT_USER_ID)
        return _BOT_USER_ID


def get_ai_bot_user_id() -> Optional[int]:
    """Cheap read of the cached id. Returns None until ensure_ai_bot_user ran."""
    return _BOT_USER_ID
