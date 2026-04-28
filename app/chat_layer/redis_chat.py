"""Redis helpers for chat: Pub/Sub publish + presence/typing keys.

Every published frame has the shape
    {"type": "<name>", "data": {...}, "timestamp": "<ISO8601>"}

For events that reference a user_id, the data block is auto-enriched with
`username` and `name` from the user-info cache, so clients don't need a
secondary lookup to render display info.
"""
import json
import logging
from datetime import datetime
from typing import List, Optional

from app.chat_layer import user_info_cache
from app.notification_layer.redis_manager import get_notification_redis

logger = logging.getLogger("app_logger")

PRESENCE_TTL = 90  # seconds — must exceed 2x heartbeat (30s)
TYPING_TTL = 5
HEARTBEAT_INTERVAL = 30


def _get_redis():
    return get_notification_redis()


def _channel(user_id: int) -> str:
    return f"chat:user:{user_id}"


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _enrich(data: dict, user_id_field: str = "user_id") -> dict:
    """Augment a payload with `username` + `name` for the user_id field.
    Idempotent: if username/name already present, leaves them alone."""
    uid = data.get(user_id_field)
    if not uid:
        return data
    if "username" in data and "name" in data:
        return data
    info = user_info_cache.get_user_info(uid)
    out = dict(data)
    out.setdefault("username", info.get("username"))
    out.setdefault("name", info.get("name"))
    return out


def _publish(user_id: int, type_: str, data: dict) -> None:
    try:
        frame = {"type": type_, "data": data, "timestamp": _now_iso()}
        _get_redis().publish(_channel(user_id), json.dumps(frame, default=str))
    except Exception as e:
        logger.error("chat publish failed user=%s type=%s err=%s", user_id, type_, e)


# ---------- Message events ----------

def publish_message_new(user_id: int, message: dict, conversation_id: int) -> None:
    # `message` is already a fully-built MessageOut (sender_username/name
    # populated server-side in messages_api). No further enrichment needed.
    _publish(user_id, "message.new", {
        "message": message, "conversation_id": conversation_id,
    })


def publish_message_edited(user_id: int, message_id: int, conversation_id: int,
                           body: str, edited_at: str) -> None:
    _publish(user_id, "message.edited", {
        "message_id": message_id, "conversation_id": conversation_id,
        "body": body, "edited_at": edited_at,
    })


def publish_message_deleted(user_id: int, message_id: int, conversation_id: int,
                            deleted_by: int) -> None:
    info = user_info_cache.get_user_info(deleted_by)
    _publish(user_id, "message.deleted", {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "deleted_by": deleted_by,
        "deleted_by_username": info.get("username"),
        "deleted_by_name": info.get("name"),
    })


def publish_message_delivered(user_id: int, message_id: int, recipient_user_id: int,
                              delivered_at: str) -> None:
    _publish(user_id, "message.delivered", _enrich({
        "message_id": message_id,
        "user_id": recipient_user_id,
        "delivered_at": delivered_at,
    }))


def publish_message_read(user_id: int, message_id: int, reader_user_id: int,
                         read_at: str) -> None:
    _publish(user_id, "message.read", _enrich({
        "message_id": message_id,
        "user_id": reader_user_id,
        "read_at": read_at,
    }))


def publish_message_read_count(user_id: int, message_id: int, conversation_id: int,
                               read_count: int) -> None:
    _publish(user_id, "message.read_count", {
        "message_id": message_id, "conversation_id": conversation_id,
        "read_count": read_count,
    })


# ---------- Typing / presence ----------

def publish_typing(user_id: int, conversation_id: int, typing_user_id: int, state: str) -> None:
    _publish(user_id, f"typing.{state}", _enrich({
        "conversation_id": conversation_id, "user_id": typing_user_id,
    }))


def publish_presence(user_id: int, target_user_id: int, status: str,
                     last_seen_at: Optional[str]) -> None:
    _publish(user_id, "presence.update", _enrich({
        "user_id": target_user_id, "status": status, "last_seen_at": last_seen_at,
    }))


# ---------- Mentions / inbox ----------

def publish_mention(user_id: int, message_id: int, conversation_id: int,
                    mentioned_by: int) -> None:
    info = user_info_cache.get_user_info(mentioned_by)
    _publish(user_id, "mention", {
        "message_id": message_id, "conversation_id": conversation_id,
        "mentioned_by": mentioned_by,
        "mentioned_by_username": info.get("username"),
        "mentioned_by_name": info.get("name"),
    })


def publish_unread_update(user_id: int, conversation_id: int,
                          unread_count: int) -> None:
    _publish(user_id, "unread.update", {
        "conversation_id": conversation_id,
        "unread_count": unread_count,
    })


def publish_inbox_bump(user_id: int, conversation_id: int,
                       latest_message: dict, unread_count: int) -> None:
    # `latest_message` is already enriched in messages_api before being passed in.
    _publish(user_id, "inbox.bump", {
        "conversation_id": conversation_id,
        "latest_message": latest_message,
        "unread_count": unread_count,
    })


def fan_out(user_ids: List[int], type_: str, data: dict) -> None:
    for uid in user_ids:
        _publish(uid, type_, data)


# ---- Presence keys ----

def set_presence_online(user_id: int) -> None:
    _get_redis().setex(f"chat:presence:{user_id}", PRESENCE_TTL, "online")


def refresh_presence(user_id: int) -> None:
    _get_redis().setex(f"chat:presence:{user_id}", PRESENCE_TTL, "online")


def get_presence(user_id: int) -> Optional[str]:
    return _get_redis().get(f"chat:presence:{user_id}")


def clear_presence(user_id: int) -> None:
    _get_redis().delete(f"chat:presence:{user_id}")


# ---- Typing keys ----

def set_typing(conversation_id: int, user_id: int) -> None:
    _get_redis().setex(f"chat:typing:{conversation_id}:{user_id}", TYPING_TTL, "1")


def clear_typing(conversation_id: int, user_id: int) -> None:
    _get_redis().delete(f"chat:typing:{conversation_id}:{user_id}")
