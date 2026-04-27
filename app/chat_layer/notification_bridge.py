"""Bridge: deliver chat messages to offline recipients via the existing
notification system. Online users go through the chat WS only."""
import json
import logging
from typing import Iterable, List

from app.chat_layer.ws_manager import ws_manager as chat_ws_manager
from app.notification_layer import redis_manager
from app.notification_layer.models import Notification, NotificationRecipient

logger = logging.getLogger("app_logger")


def _is_online(user_id: int) -> bool:
    return chat_ws_manager.is_online(user_id)


def _preview(message) -> str:
    body = (getattr(message, "body", None) or "").strip()
    if body:
        return body[:140]
    mt = getattr(message, "message_type", "text")
    return {"image": "[image]", "voice": "[voice note]", "file": "[file]"}.get(mt, "[message]")


def _insert_notification(db, *, recipient_id: int, sender: dict, message,
                         conversation_id: int, event_name: str,
                         priority: str = "medium") -> int:
    sender_name = sender.get("name") or sender.get("username") or "Someone"
    title = (f"{sender_name} mentioned you" if event_name == "chat.mention"
             else f"New message from {sender_name}")
    notif = Notification(
        title=title,
        message=_preview(message),
        delivery_mode="push",
        domain_type="chat",
        visibility="personal",
        priority=priority,
        target_type="user",
        target_id=str(recipient_id),
        source_service="chat",
        event_type=event_name,
        extra_metadata=json.dumps({
            "conversation_id": conversation_id,
            "message_id": getattr(message, "id", None),
            "sender_id": sender.get("user_id"),
        }),
        created_by=sender.get("user_id"),
        is_active=1,
    )
    db.add(notif)
    db.flush()
    db.add(NotificationRecipient(notification_id=notif.id, user_id=recipient_id,
                                 is_read=0))
    db.commit()
    return notif.id


def _publish_to_notification_ws(user_id: int, notif_id: int, *, title: str,
                                preview: str, conversation_id: int,
                                message_id: int) -> None:
    payload = {
        "id": notif_id,
        "title": title,
        "message": preview,
        "delivery_mode": "push",
        "domain_type": "chat",
        "metadata": {
            "conversation_id": conversation_id,
            "message_id": message_id,
        },
    }
    try:
        redis_manager.publish_to_user(user_id=user_id, payload=payload)
    except Exception as e:
        logger.error("publish to notif user=%s: %s", user_id, e)


def handle_message_for_offline_recipients(*, db, conversation_id: int,
                                          message, sender: dict,
                                          recipients: Iterable[int]) -> None:
    sender_name = sender.get("name") or sender.get("username") or "Someone"
    title = f"New message from {sender_name}"
    preview = _preview(message)
    for uid in recipients:
        if _is_online(uid):
            continue
        try:
            notif_id = _insert_notification(
                db, recipient_id=uid, sender=sender, message=message,
                conversation_id=conversation_id, event_name="chat.message_received",
            )
            _publish_to_notification_ws(uid, notif_id, title=title, preview=preview,
                                        conversation_id=conversation_id,
                                        message_id=getattr(message, "id", 0))
        except Exception as e:
            logger.error("offline notification failed user=%s: %s", uid, e)


def handle_mention_for_users(*, db, message, sender: dict,
                             mentioned_user_ids: List[int]) -> None:
    sender_name = sender.get("name") or sender.get("username") or "Someone"
    preview = _preview(message)
    for uid in mentioned_user_ids:
        try:
            notif_id = _insert_notification(
                db, recipient_id=uid, sender=sender, message=message,
                conversation_id=getattr(message, "conversation_id", 0),
                event_name="chat.mention", priority="high",
            )
            _publish_to_notification_ws(
                uid, notif_id, title=f"{sender_name} mentioned you",
                preview=preview,
                conversation_id=getattr(message, "conversation_id", 0),
                message_id=getattr(message, "id", 0),
            )
        except Exception as e:
            logger.error("mention notification failed user=%s: %s", uid, e)
