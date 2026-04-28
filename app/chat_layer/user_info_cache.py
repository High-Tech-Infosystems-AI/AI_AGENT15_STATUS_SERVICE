"""In-process + Redis-backed cache of `user_id → {id, username, name}`.

Every chat publisher calls `get_user_info(user_id)` so events arrive at the
client already enriched with display info — clients no longer need to call
`/auth/users/{id}` to render names. The two-tier cache keeps this cheap:

  - L1 (in-process LRU, 5 min TTL) — single-worker hot path, no I/O.
  - L2 (Redis, 10 min TTL)         — shared across uvicorn workers + pods.
  - L3 (MySQL `users` table)       — authoritative, slow path.

Cache is invalidated implicitly by TTL; we don't ship explicit invalidation
because user names rarely change and stale display info for ≤ 10 min is
acceptable.
"""
import json
import logging
import time
from threading import Lock
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.notification_layer.redis_manager import get_notification_redis

logger = logging.getLogger("app_logger")

# ---- L1: in-process ----
_MEMO: dict = {}                    # {user_id: info}
_MEMO_EXPIRY: dict = {}             # {user_id: float}
_LOCK = Lock()
_MEMO_MAX = 4096
_MEMO_TTL_SECONDS = 300

# ---- L2: Redis ----
_REDIS_KEY = "chat:user_info:{}"
_REDIS_TTL_SECONDS = 600

_UNKNOWN = {"id": 0, "username": "unknown", "name": "Unknown User"}


def _fallback(user_id: int) -> dict:
    return {"id": user_id, "username": f"u{user_id}", "name": f"User {user_id}"}


def _memo_get(user_id: int) -> Optional[dict]:
    now = time.time()
    with _LOCK:
        info = _MEMO.get(user_id)
        if not info:
            return None
        if _MEMO_EXPIRY.get(user_id, 0) <= now:
            _MEMO.pop(user_id, None)
            _MEMO_EXPIRY.pop(user_id, None)
            return None
        return info


def _memo_put(user_id: int, info: dict) -> None:
    with _LOCK:
        if len(_MEMO) >= _MEMO_MAX:
            _MEMO.clear()
            _MEMO_EXPIRY.clear()
        _MEMO[user_id] = info
        _MEMO_EXPIRY[user_id] = time.time() + _MEMO_TTL_SECONDS


def _redis_get(user_id: int) -> Optional[dict]:
    try:
        val = get_notification_redis().get(_REDIS_KEY.format(user_id))
        if val:
            return json.loads(val)
    except Exception as exc:
        logger.debug("user_info_cache redis get failed user=%s: %s", user_id, exc)
    return None


def _redis_put(user_id: int, info: dict) -> None:
    try:
        get_notification_redis().setex(
            _REDIS_KEY.format(user_id),
            _REDIS_TTL_SECONDS,
            json.dumps(info, default=str),
        )
    except Exception as exc:
        logger.debug("user_info_cache redis put failed user=%s: %s", user_id, exc)


def _db_fetch(user_id: int, db: Optional[Session]) -> Optional[dict]:
    """Look up `users` row. Creates a transient session if `db` is None.
    Returns None if the user doesn't exist or the lookup fails."""
    own_session = False
    if db is None:
        from app.database_Layer.db_config import SessionLocal
        db = SessionLocal()
        own_session = True
    try:
        row = db.execute(
            text("SELECT id, username, name FROM users WHERE id = :uid"),
            {"uid": user_id},
        ).first()
        if row:
            m = row._mapping
            return {"id": m["id"], "username": m["username"], "name": m["name"]}
        return None
    except Exception as exc:
        logger.warning("user_info_cache db fetch failed user=%s: %s", user_id, exc)
        return None
    finally:
        if own_session:
            db.close()


def get_user_info(user_id: Optional[int], db: Optional[Session] = None) -> dict:
    """Resolve a user_id to {id, username, name}.

    Misses fall back through L1 → L2 → L3 in order. Always returns a dict
    so callers don't have to None-check; an unresolvable id returns a
    placeholder so events still serialise cleanly.
    """
    if not user_id:
        return _UNKNOWN

    info = _memo_get(user_id)
    if info:
        return info

    info = _redis_get(user_id)
    if info:
        _memo_put(user_id, info)
        return info

    info = _db_fetch(user_id, db)
    if info:
        _memo_put(user_id, info)
        _redis_put(user_id, info)
        return info

    return _fallback(user_id)


def invalidate(user_id: int) -> None:
    """Drop both cache layers for a user. Call after profile updates that
    change `username` or `name`."""
    with _LOCK:
        _MEMO.pop(user_id, None)
        _MEMO_EXPIRY.pop(user_id, None)
    try:
        get_notification_redis().delete(_REDIS_KEY.format(user_id))
    except Exception:
        pass
