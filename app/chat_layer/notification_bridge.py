"""Bridge between chat events and the notification system.

Everything that should land in a user's notification feed and/or browser push
goes through one of the `handle_*` functions here. Each handler:

  1. Builds a sender display name and a conversation-context string ("DM",
     "team Engineering", "#general") so titles never read as "Someone".
  2. Inserts a per-recipient `Notification` row with metadata that carries
     the click-through deep link (`link`), `conversation_id`, and `message_id`.
  3. Publishes the same payload over the in-app notification WS so an open
     tab gets it instantly.
  4. Sends it via Web Push (VAPID) so closed tabs and OS-level notifications
     also receive it.

We always send to every recipient regardless of online state — push+WS are
idempotent on the client (the SW dedupes by `tag = "chat-conv-{id}"` so the
same message can't double-show).
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, List, Optional

from sqlalchemy import text

from app.chat_layer import push_service, user_info_cache
from app.notification_layer import redis_manager
from app.notification_layer.models import Notification, NotificationRecipient
from app.core import settings

logger = logging.getLogger("app_logger")


# ---------- Display helpers ----------

def _resolve_sender_name(db, sender: dict) -> str:
    """Pull the freshest display name we can. Order:
       sender.name -> sender.username -> users.name -> users.username -> "User <id>".
    Never returns "Someone"."""
    name = (sender.get("name") or "").strip()
    if name:
        return name
    uname = (sender.get("username") or "").strip()
    if uname:
        return uname
    uid = sender.get("user_id")
    if uid:
        info = user_info_cache.get_user_info(uid, db=db)
        return info.get("name") or info.get("username") or f"User {uid}"
    return "User"


def _conversation_context(db, conversation_id: int) -> dict:
    """Return {type, team_id, team_name, title} for the conversation. Used to
    decorate notification titles (e.g. "Engineering · Alice")."""
    row = db.execute(
        text("""SELECT c.type, c.team_id, c.title, t.name AS team_name
                  FROM chat_conversations c
             LEFT JOIN teams t ON t.id = c.team_id
                 WHERE c.id = :cid"""),
        {"cid": conversation_id},
    ).first()
    if not row:
        return {"type": "dm", "team_id": None, "team_name": None, "title": None}
    m = row._mapping
    return {
        "type": m["type"],
        "team_id": m["team_id"],
        "team_name": m["team_name"],
        "title": m["title"],
    }


def _conv_label(ctx: dict) -> str:
    """Short label for the conversation, used as a title prefix."""
    if ctx["type"] == "team":
        return ctx["team_name"] or ctx["title"] or "Team"
    if ctx["type"] == "general":
        return ctx["title"] or "#general"
    return ""  # DM — no prefix needed, sender name carries it


def _build_title(*, event: str, sender_name: str, ctx: dict) -> str:
    """Examples:
       new message in DM:    "Alice Sharma"
       new message in team:  "Engineering · Alice Sharma"
       new message in general: "#general · Alice Sharma"
       mention:              "Alice Sharma mentioned you in Engineering"
       edit (team):          "Engineering · Alice Sharma edited a message"
       delete (team):        "Engineering · Alice Sharma deleted a message"
       reaction:             "Alice Sharma reacted 👍 to your message"
    """
    label = _conv_label(ctx)
    prefix = f"{label} · " if label else ""
    if event == "chat.mention":
        where = f" in {label}" if label else ""
        return f"{sender_name} mentioned you{where}"
    if event == "chat.message_edited":
        return f"{prefix}{sender_name} edited a message"
    if event == "chat.message_deleted":
        return f"{prefix}{sender_name} deleted a message"
    if event == "chat.message_forwarded":
        return f"{prefix}{sender_name} forwarded a message"
    if event == "chat.reaction_added":
        return f"{sender_name} reacted to your message"
    return f"{prefix}{sender_name}"


def _preview(message) -> str:
    body = (getattr(message, "body", None) or "").strip()
    if body:
        return body[:140]
    mt = getattr(message, "message_type", "text")
    return {"image": "[image]", "voice": "[voice note]",
            "file": "[file]"}.get(mt, "[message]")


def _build_link(conversation_id: int, message_id: Optional[int]) -> str:
    """Canonical in-app deep link for the click-through. Frontend reads this
    on `notificationclick` and routes to the chat with the message focused.

    We return an absolute URL when WEB_PUSH_FRONTEND_BASE_URL is configured
    (needed for OS-level push to open the right tab) and a relative path
    otherwise (the in-app dropdown navigates within the same origin).
    """
    base = (getattr(settings, "WEB_PUSH_FRONTEND_BASE_URL", "") or "").rstrip("/")
    qs = f"?message_id={message_id}" if message_id else ""
    path = f"/chat?conversation_id={conversation_id}"
    if message_id:
        path = f"/chat?conversation_id={conversation_id}&message_id={message_id}"
    return f"{base}{path}" if base else path


# ---------- Persistence + delivery ----------

def _insert_notification(db, *, recipient_id: int, sender_id: Optional[int],
                         title: str, body_preview: str, conversation_id: int,
                         message_id: Optional[int], event_name: str,
                         link: str, priority: str = "medium") -> int:
    notif = Notification(
        title=title,
        message=body_preview,
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
            "message_id": message_id,
            "sender_id": sender_id,
            "link": link,
        }),
        created_by=sender_id,
        is_active=1,
    )
    db.add(notif)
    db.flush()
    db.add(NotificationRecipient(notification_id=notif.id, user_id=recipient_id,
                                 is_read=0))
    db.commit()
    return notif.id


def _publish_in_app(user_id: int, *, notif_id: int, title: str, body: str,
                    conversation_id: int, message_id: Optional[int],
                    link: str, event: str) -> None:
    """In-app WS payload (notification feed dropdown). Uses redis pub/sub via
    the existing notification_layer manager."""
    payload = {
        "id": notif_id,
        "title": title,
        "message": body,
        "delivery_mode": "push",
        "domain_type": "chat",
        "event_type": event,
        "metadata": {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "link": link,
        },
    }
    try:
        redis_manager.publish_to_user(user_id=user_id, payload=payload)
    except Exception as e:
        logger.error("publish to in-app notif user=%s: %s", user_id, e)


def _push_payload(*, title: str, body: str, conversation_id: int,
                  message_id: Optional[int], link: str, event: str,
                  notif_id: int) -> dict:
    """Shape consumed by the service worker in `public/sw.js`. Keep the
    `tag` deterministic so the OS dedupes consecutive messages in the same
    conversation into a single notification stack."""
    return {
        "title": title,
        "body": body,
        "tag": f"chat-conv-{conversation_id}",
        "renotify": True,
        "icon": "/notification/notificationIcons.png",
        "badge": "/notification/bellIcon.png",
        "data": {
            "notif_id": notif_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "link": link,
            "event": event,
        },
    }


def _deliver_to_recipients(db, *, recipient_ids: Iterable[int], sender_id: Optional[int],
                           sender_name: str, ctx: dict, conversation_id: int,
                           message_id: Optional[int], body_preview: str,
                           event: str, priority: str = "medium") -> None:
    title = _build_title(event=event, sender_name=sender_name, ctx=ctx)
    link = _build_link(conversation_id, message_id)
    for uid in recipient_ids:
        try:
            notif_id = _insert_notification(
                db, recipient_id=uid, sender_id=sender_id, title=title,
                body_preview=body_preview, conversation_id=conversation_id,
                message_id=message_id, event_name=event, link=link,
                priority=priority,
            )
        except Exception as e:
            logger.error("notification row insert failed user=%s: %s", uid, e)
            continue
        _publish_in_app(uid, notif_id=notif_id, title=title, body=body_preview,
                        conversation_id=conversation_id, message_id=message_id,
                        link=link, event=event)
        try:
            push_service.send_to_user(db, uid, _push_payload(
                title=title, body=body_preview, conversation_id=conversation_id,
                message_id=message_id, link=link, event=event, notif_id=notif_id,
            ))
        except Exception as e:
            logger.error("web-push fan-out failed user=%s: %s", uid, e)


# ---------- Public handlers (called from messages_api) ----------

def handle_message_for_recipients(*, db, conversation_id: int, message,
                                  sender: dict, recipients: Iterable[int]) -> None:
    """A new message was sent. Notify everyone except the sender, on every
    delivery channel (DB row + in-app WS + web push)."""
    ctx = _conversation_context(db, conversation_id)
    sender_name = _resolve_sender_name(db, sender)
    body_preview = _preview(message)
    _deliver_to_recipients(
        db, recipient_ids=list(recipients),
        sender_id=sender.get("user_id"),
        sender_name=sender_name, ctx=ctx,
        conversation_id=conversation_id,
        message_id=getattr(message, "id", None),
        body_preview=body_preview,
        event="chat.message_received",
    )


# Back-compat shim — older imports may still call the original name.
handle_message_for_offline_recipients = handle_message_for_recipients


def handle_mention_for_users(*, db, message, sender: dict,
                             mentioned_user_ids: List[int]) -> None:
    if not mentioned_user_ids:
        return
    conversation_id = getattr(message, "conversation_id", 0)
    ctx = _conversation_context(db, conversation_id)
    sender_name = _resolve_sender_name(db, sender)
    _deliver_to_recipients(
        db, recipient_ids=mentioned_user_ids,
        sender_id=sender.get("user_id"),
        sender_name=sender_name, ctx=ctx,
        conversation_id=conversation_id,
        message_id=getattr(message, "id", None),
        body_preview=_preview(message),
        event="chat.mention", priority="high",
    )


def handle_message_edited(*, db, message, sender: dict,
                          recipients: Iterable[int]) -> None:
    """The sender edited a message. Notify everyone else in the conversation
    so their UI knows to re-render and so a closed tab gets a quiet ping."""
    conversation_id = getattr(message, "conversation_id", 0)
    ctx = _conversation_context(db, conversation_id)
    sender_name = _resolve_sender_name(db, sender)
    _deliver_to_recipients(
        db, recipient_ids=[r for r in recipients if r != sender.get("user_id")],
        sender_id=sender.get("user_id"),
        sender_name=sender_name, ctx=ctx,
        conversation_id=conversation_id,
        message_id=getattr(message, "id", None),
        body_preview=_preview(message),
        event="chat.message_edited", priority="low",
    )


def handle_message_deleted(*, db, message, sender: dict,
                           recipients: Iterable[int]) -> None:
    conversation_id = getattr(message, "conversation_id", 0)
    ctx = _conversation_context(db, conversation_id)
    sender_name = _resolve_sender_name(db, sender)
    _deliver_to_recipients(
        db, recipient_ids=[r for r in recipients if r != sender.get("user_id")],
        sender_id=sender.get("user_id"),
        sender_name=sender_name, ctx=ctx,
        conversation_id=conversation_id,
        message_id=getattr(message, "id", None),
        body_preview="[message deleted]",
        event="chat.message_deleted", priority="low",
    )


def handle_message_forwarded(*, db, conversation_id: int, message,
                             sender: dict, recipients: Iterable[int]) -> None:
    """A message was forwarded into `conversation_id`. Recipients of that
    destination conversation get the notification."""
    ctx = _conversation_context(db, conversation_id)
    sender_name = _resolve_sender_name(db, sender)
    _deliver_to_recipients(
        db, recipient_ids=list(recipients),
        sender_id=sender.get("user_id"),
        sender_name=sender_name, ctx=ctx,
        conversation_id=conversation_id,
        message_id=getattr(message, "id", None),
        body_preview=_preview(message),
        event="chat.message_forwarded",
    )
