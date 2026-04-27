"""Presence helpers - compute visibility set, fan out updates."""
from datetime import datetime
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.chat_layer import redis_chat


def _fetch_co_members(db: Session, user_id: int) -> List[int]:
    """All users who share at least one conversation with user_id."""
    rows = db.execute(text("""
        SELECT DISTINCT m2.user_id
        FROM chat_conversation_members m1
        JOIN chat_conversation_members m2
          ON m1.conversation_id = m2.conversation_id
        WHERE m1.user_id = :uid AND m2.user_id <> :uid
    """), {"uid": user_id}).all()
    return [r[0] for r in rows]


def compute_visible_to(db: Session, user_id: int) -> List[int]:
    return _fetch_co_members(db, user_id)


def fan_out_presence(*, db: Session, user_id: int, status: str,
                     last_seen_at: Optional[datetime]) -> None:
    last_seen_iso = last_seen_at.isoformat() if last_seen_at else None
    for uid in _fetch_co_members(db, user_id):
        redis_chat.publish_presence(
            user_id=uid, target_user_id=user_id,
            status=status, last_seen_at=last_seen_iso,
        )
