"""Presence helpers - compute visibility set, fan out updates."""
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.chat_layer import redis_chat

logger = logging.getLogger("app_logger")


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


def announce_presence_to(*, db: Session,
                         target_user_ids: List[int],
                         about_user_ids: List[int]) -> None:
    """Send `presence.update` events about `about_user_ids` to every user in
    `target_user_ids`. Use this when membership changes (new DM, team chat
    creation, member added) so the affected users learn each other's status
    immediately rather than waiting for the next reconnect.

    Self-pairs (target == about) are skipped automatically. Reads online
    status from Redis (one MGET) and `last_seen_at` from the DB (one bulk
    SELECT) — efficient even when one set is large.
    """
    targets = list({uid for uid in target_user_ids if uid})
    abouts = list({uid for uid in about_user_ids if uid})
    if not targets or not abouts:
        return

    # Redis: who's currently online?
    online = set()
    try:
        client = redis_chat._get_redis()
        keys = [f"chat:presence:{uid}" for uid in abouts]
        values = client.mget(*keys)
        for uid, val in zip(abouts, values):
            if val:
                online.add(uid)
    except Exception as exc:
        logger.warning("announce_presence_to redis read failed: %s", exc)

    # DB: last_seen_at for everyone in `abouts`
    last_seen: dict = {}
    try:
        rows = db.execute(
            text(
                "SELECT user_id, last_seen_at FROM chat_user_presence "
                "WHERE user_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": abouts},
        ).all()
        for r in rows:
            m = r._mapping
            last_seen[m["user_id"]] = m["last_seen_at"]
    except Exception as exc:
        logger.warning("announce_presence_to db read failed: %s", exc)

    for target in targets:
        for about in abouts:
            if target == about:
                continue
            ls = last_seen.get(about)
            redis_chat.publish_presence(
                user_id=target,
                target_user_id=about,
                status="online" if about in online else "offline",
                last_seen_at=ls.isoformat() if ls else None,
            )


def get_presence_snapshot(db: Session, user_id: int) -> List[dict]:
    """Return the current presence of every user who shares a conversation
    with user_id. Online status from Redis (authoritative); last_seen_at
    from the DB. Use this at WS-connect time so a user who joins after
    others were already online still sees their `online` dots immediately.
    """
    co_members = _fetch_co_members(db, user_id)
    if not co_members:
        return []

    # Authoritative online flag from Redis (key exists ⇔ heartbeat alive)
    online_ids = set()
    try:
        client = redis_chat._get_redis()
        keys = [f"chat:presence:{uid}" for uid in co_members]
        values = client.mget(*keys)
        for uid, val in zip(co_members, values):
            if val:
                online_ids.add(uid)
    except Exception as exc:
        logger.warning("presence snapshot redis read failed: %s", exc)

    # last_seen_at for everyone (only used for offline rows)
    last_seen_by_id: dict = {}
    try:
        rows = db.execute(
            text(
                "SELECT user_id, last_seen_at FROM chat_user_presence "
                "WHERE user_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": co_members},
        ).all()
        for r in rows:
            m = r._mapping
            last_seen_by_id[m["user_id"]] = m["last_seen_at"]
    except Exception as exc:
        logger.warning("presence snapshot db read failed: %s", exc)

    # Enrich with username + name in one batched users-table query
    user_info_by_id: dict = {}
    if co_members:
        try:
            rows = db.execute(
                text(
                    "SELECT id, username, name FROM users WHERE id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": co_members},
            ).all()
            for r in rows:
                m = r._mapping
                user_info_by_id[m["id"]] = {"username": m["username"], "name": m["name"]}
        except Exception as exc:
            logger.warning("presence snapshot users batch read failed: %s", exc)

    snapshot = []
    for uid in co_members:
        is_online = uid in online_ids
        last_seen = last_seen_by_id.get(uid)
        info = user_info_by_id.get(uid, {})
        snapshot.append({
            "user_id": uid,
            "username": info.get("username"),
            "name": info.get("name"),
            "status": "online" if is_online else "offline",
            "last_seen_at": last_seen.isoformat() if last_seen else None,
        })
    return snapshot
