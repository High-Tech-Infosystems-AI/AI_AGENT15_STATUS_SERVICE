"""Redis helpers for chat: Pub/Sub publish + presence/typing keys."""
import json
import logging
from typing import List, Optional

from app.notification_layer.redis_manager import get_notification_redis

logger = logging.getLogger("app_logger")

PRESENCE_TTL = 90  # seconds — must exceed 2x heartbeat (30s)
TYPING_TTL = 5
HEARTBEAT_INTERVAL = 30


def _get_redis():
    return get_notification_redis()


def _channel(user_id: int) -> str:
    return f"chat:user:{user_id}"


def _publish(user_id: int, type_: str, data: dict) -> None:
    try:
        payload = json.dumps({"type": type_, "data": data}, default=str)
        _get_redis().publish(_channel(user_id), payload)
    except Exception as e:
        logger.error("chat publish failed user=%s type=%s err=%s", user_id, type_, e)


def publish_message_new(user_id: int, message: dict, conversation_id: int) -> None:
    _publish(user_id, "message.new", {"message": message, "conversation_id": conversation_id})


def publish_message_edited(user_id: int, message_id: int, conversation_id: int,
                           body: str, edited_at: str) -> None:
    _publish(user_id, "message.edited", {
        "message_id": message_id, "conversation_id": conversation_id,
        "body": body, "edited_at": edited_at,
    })


def publish_message_deleted(user_id: int, message_id: int, conversation_id: int,
                            deleted_by: int) -> None:
    _publish(user_id, "message.deleted", {
        "message_id": message_id, "conversation_id": conversation_id, "deleted_by": deleted_by,
    })


def publish_message_delivered(user_id: int, message_id: int, recipient_user_id: int,
                              delivered_at: str) -> None:
    _publish(user_id, "message.delivered", {
        "message_id": message_id, "user_id": recipient_user_id, "delivered_at": delivered_at,
    })


def publish_message_read(user_id: int, message_id: int, reader_user_id: int,
                         read_at: str) -> None:
    _publish(user_id, "message.read", {
        "message_id": message_id, "user_id": reader_user_id, "read_at": read_at,
    })


def publish_message_read_count(user_id: int, message_id: int, conversation_id: int,
                               read_count: int) -> None:
    _publish(user_id, "message.read_count", {
        "message_id": message_id, "conversation_id": conversation_id, "read_count": read_count,
    })


def publish_typing(user_id: int, conversation_id: int, typing_user_id: int, state: str) -> None:
    _publish(user_id, f"typing.{state}", {
        "conversation_id": conversation_id, "user_id": typing_user_id,
    })


def publish_presence(user_id: int, target_user_id: int, status: str,
                     last_seen_at: Optional[str]) -> None:
    _publish(user_id, "presence.update", {
        "user_id": target_user_id, "status": status, "last_seen_at": last_seen_at,
    })


def publish_mention(user_id: int, message_id: int, conversation_id: int,
                    mentioned_by: int) -> None:
    _publish(user_id, "mention", {
        "message_id": message_id, "conversation_id": conversation_id,
        "mentioned_by": mentioned_by,
    })


def publish_unread_update(user_id: int, conversation_id: int,
                          unread_count: int) -> None:
    """Loopback: when the user reads (on tab A), update their other tabs (B, C…)
    so the inbox badge clears everywhere."""
    _publish(user_id, "unread.update", {
        "conversation_id": conversation_id,
        "unread_count": unread_count,
    })


def publish_inbox_bump(user_id: int, conversation_id: int,
                       latest_message: dict, unread_count: int) -> None:
    """Inbox-cell update: same data the inbox endpoint would return for one
    row. Sent when a new message arrives so clients can update the cell
    (preview + timestamp + unread badge) without refetching the whole inbox."""
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
