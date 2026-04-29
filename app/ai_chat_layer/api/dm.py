"""Helpers around the per-user AI Assistant DM thread.

Every user has exactly one DM with the synthetic `ai_assistant` user.
This module owns the lookup/provisioning of that conversation so the
streaming endpoint and the schedule worker can hand off without
duplicating logic.
"""
from __future__ import annotations

from typing import Tuple

from sqlalchemy.orm import Session

from app.ai_chat_layer.system_bot import ensure_ai_bot_user
from app.chat_layer import store as chat_store


def get_or_create_ai_dm(db: Session, user_id: int) -> Tuple[int, int]:
    """Return (conversation_id, ai_bot_user_id), creating the DM if missing."""
    bot_id = ensure_ai_bot_user(db)
    if user_id == bot_id:
        raise ValueError("AI bot cannot DM itself")
    conv, _ = chat_store.get_or_create_dm(db, user_id, bot_id)
    return conv.id, bot_id
