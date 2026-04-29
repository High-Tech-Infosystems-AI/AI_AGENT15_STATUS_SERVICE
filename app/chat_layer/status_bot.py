"""Status Bot — the synthetic user that posts /status replies.

The bot is a real row in `users` so foreign-key constraints (sender_id,
conversation membership) work without special-casing. It's reserved by a
distinguishing `username = "status_bot"` and `enable = 0` so it never shows
up in the people picker or login flows.

Bootstrap is idempotent: call `ensure_status_bot_user()` once at chat
service startup, get back the user_id; subsequent calls are cheap.

Slash-command parsing is also here so `messages_api` can stay focused on
the request handler.
"""
from __future__ import annotations

import logging
import re
from threading import Lock
from typing import List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

_BOT_USERNAME = "status_bot"
_BOT_DISPLAY_NAME = "Status Bot"
_BOT_USER_ID: Optional[int] = None
_BOT_LOCK = Lock()

# Loose @@ref:type:id@@ token. Type is one of the ENTITY_TYPES; id is
# numeric or slug-like.
_REF_TOKEN_RE = re.compile(
    r"@@ref:(?P<type>[a-z]+):(?P<id>[A-Za-z0-9_-]+)@@",
)


def ensure_status_bot_user(db: Session) -> int:
    """Return the bot's user_id, creating the row if missing. Cached
    in-process after the first call."""
    global _BOT_USER_ID
    with _BOT_LOCK:
        if _BOT_USER_ID is not None:
            return _BOT_USER_ID

        row = db.execute(
            text("SELECT id FROM users WHERE username = :u LIMIT 1"),
            {"u": _BOT_USERNAME},
        ).first()
        if row:
            _BOT_USER_ID = int(row[0])
            return _BOT_USER_ID

        # Create. We try to reuse the smallest column set so this works
        # even if the users table has lots of NOT NULL columns we don't
        # care about — fields below match what the existing schema needs.
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
                    "name": _BOT_DISPLAY_NAME,
                    "uname": _BOT_USERNAME,
                    "email": "status-bot@chat.local",
                    "pwd": "!disabled!",
                },
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("status bot user insert (full schema) failed: %s — "
                           "trying minimal insert", e)
            db.execute(
                text("""
                    INSERT INTO users (name, username, email, enable)
                    VALUES (:name, :uname, :email, 0)
                """),
                {
                    "name": _BOT_DISPLAY_NAME,
                    "uname": _BOT_USERNAME,
                    "email": "status-bot@chat.local",
                },
            )
            db.commit()

        row = db.execute(
            text("SELECT id FROM users WHERE username = :u LIMIT 1"),
            {"u": _BOT_USERNAME},
        ).first()
        if not row:
            raise RuntimeError("Failed to create Status Bot user")
        _BOT_USER_ID = int(row[0])
        logger.info("Status Bot user provisioned id=%s", _BOT_USER_ID)
        return _BOT_USER_ID


def find_status_command(body: Optional[str]) -> Optional[List[Tuple[str, str]]]:
    """If the message is a `/status` command (i.e. body starts with
    `/status` and contains at least one @@ref:...@@ token), return the
    list of `(type, id)` tuples to resolve. Returns None otherwise.
    """
    if not body:
        return None
    stripped = body.strip()
    if not stripped.lower().startswith("/status"):
        return None
    refs = [(m.group("type"), m.group("id"))
            for m in _REF_TOKEN_RE.finditer(stripped)]
    return refs if refs else None
