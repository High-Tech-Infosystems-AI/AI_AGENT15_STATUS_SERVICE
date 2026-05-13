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
    in-process after the first call. Schema-aware INSERT — adapts to the
    live `users` column set."""
    global _BOT_USER_ID
    with _BOT_LOCK:
        if _BOT_USER_ID is not None:
            return _BOT_USER_ID

        from app.chat_layer._synthetic_user import provision_synthetic_user
        try:
            _BOT_USER_ID = provision_synthetic_user(
                db,
                username=_BOT_USERNAME,
                display_name=_BOT_DISPLAY_NAME,
                email="status-bot@chat.local",
            )
        except Exception:
            db.rollback()
            raise
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
