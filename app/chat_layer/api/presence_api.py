"""Presence endpoint.

Online status is read from Redis (the heartbeat-TTL key is the source of
truth — it expires within 90s of the last `ping`). The DB row in
`chat_user_presence` is only authoritative for `last_seen_at`. Reading
from the DB alone gives stale results when the user just reconnected
faster than the row was rewritten, or when the row says "online" but the
heartbeat has expired (network blip, hard close).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.chat_layer import redis_chat, user_info_cache
from app.chat_layer.auth import current_user
from app.chat_layer.models import ChatUserPresence
from app.chat_layer.schemas import PresenceOut
from app.database_Layer.db_config import SessionLocal

logger = logging.getLogger("app_logger")
router = APIRouter()


def _fetch_db_row(db, user_id: int) -> Optional[ChatUserPresence]:
    return db.get(ChatUserPresence, user_id)


def _redis_says_online(user_id: int) -> bool:
    """Returns True when the user has a live heartbeat key in Redis."""
    try:
        return bool(redis_chat.get_presence(user_id))
    except Exception as exc:
        logger.warning("redis presence check failed user=%s: %s", user_id, exc)
        return False


@router.get("/users/{user_id}/presence", response_model=PresenceOut)
def get_presence(user_id: int, user: dict = Depends(current_user)):
    """Authoritative current presence for one user.

    online status: Redis `chat:presence:{user_id}` key existence (90s TTL).
    last_seen_at:  DB `chat_user_presence.last_seen_at` (written on disconnect).
    name/username: pulled from the user-info cache so the client never has to
                   round-trip RBAC for the display string.
    """
    online = _redis_says_online(user_id)
    db = SessionLocal()
    try:
        row = _fetch_db_row(db, user_id)
        info = user_info_cache.get_user_info(user_id, db=db)
        last_seen = row.last_seen_at if row else None
        return JSONResponse(
            content={
                "user_id": user_id,
                "username": info.get("username"),
                "name": info.get("name"),
                "status": "online" if online else "offline",
                "last_seen_at": last_seen.isoformat() if last_seen else None,
            },
        )
    finally:
        db.close()
