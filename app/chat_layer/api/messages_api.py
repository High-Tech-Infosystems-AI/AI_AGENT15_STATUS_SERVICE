"""Message endpoints: send, list, mark-read, edit, delete, forward."""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import bindparam, text

import app.chat_layer.notification_bridge as bridge
from app.chat_layer import redis_chat, s3_chat_service as s3, store, user_info_cache
from app.chat_layer.auth import current_user
from app.chat_layer.ws_manager import ws_manager as chat_ws_manager
from app.chat_layer.chat_acl import (
    can_delete_message, can_edit_message, can_post_dm, can_post_general, can_post_team,
)
from app.chat_layer.formatting import sanitise_body
from app.chat_layer.mentions import extract_usernames
from app.chat_layer.models import (
    ChatConversation, ChatMessage, ChatMessageAttachment,
)
from app.chat_layer.schemas import (
    AddReactionRequest, AttachmentOut, EditMessageRequest, ErrorResponse,
    ForwardMessageRequest, MarkReadBulkRequest,
    MessageOut, PaginatedMessages, SendMessageRequest,
)
from app.database_Layer.db_config import SessionLocal
from app.database_Layer.db_model import User

logger = logging.getLogger("app_logger")
router = APIRouter()


def _err(code: str, msg: str, status_: int) -> JSONResponse:
    return JSONResponse(status_code=status_, content={"error_code": code, "message": msg})


def _fetch_attachment(db, att_id: Optional[int]):
    if att_id is None:
        return None
    return db.get(ChatMessageAttachment, att_id)


def _attachment_out(att) -> Optional[AttachmentOut]:
    if att is None:
        return None
    return AttachmentOut(
        id=att.id, mime_type=att.mime_type, file_name=att.file_name,
        size_bytes=att.size_bytes, duration_seconds=att.duration_seconds,
        waveform_json=att.waveform_json, url=s3.presign_get(att.s3_key),
        thumbnail_url=s3.presign_get(att.thumbnail_s3_key) if att.thumbnail_s3_key else None,
    )


def _to_message_out(msg, attachment=None, mention_ids=None, db=None,
                    read_by=None, delivered_to=None, reactions=None) -> dict:
    body_out = msg.body
    if msg.deleted_at is not None:
        body_out = "[message deleted]"
        attachment = None
    att_out = attachment if isinstance(attachment, AttachmentOut) else _attachment_out(attachment)
    sender_info = user_info_cache.get_user_info(msg.sender_id, db=db)
    rb = list(read_by or [])
    dt = list(delivered_to or [])
    # Forwarded-from info: resolve the original sender's name when present.
    fwd_sender_id = getattr(msg, "forwarded_from_sender_id", None)
    fwd_sender_username = None
    fwd_sender_name = None
    if fwd_sender_id:
        fwd_info = user_info_cache.get_user_info(fwd_sender_id, db=db)
        fwd_sender_username = fwd_info.get("username")
        fwd_sender_name = fwd_info.get("name")
    return MessageOut(
        id=msg.id, conversation_id=msg.conversation_id, sender_id=msg.sender_id,
        sender_username=sender_info.get("username"),
        sender_name=sender_info.get("name"),
        message_type=msg.message_type, body=body_out, attachment=att_out,
        reply_to_message_id=msg.reply_to_message_id,
        forwarded_from_message_id=msg.forwarded_from_message_id,
        forwarded_from_sender_id=fwd_sender_id,
        forwarded_from_sender_username=fwd_sender_username,
        forwarded_from_sender_name=fwd_sender_name,
        edited_at=msg.edited_at, deleted_at=msg.deleted_at,
        created_at=msg.created_at, mentions=list(mention_ids or []),
        read_by=rb, delivered_to=dt,
        read_count=len(rb) if rb else None,
        delivered_count=len(dt) if dt else None,
        reactions=reactions or [],
    ).model_dump(mode="json")


def _fetch_message_and_conv(db, message_id: int):
    m = db.get(ChatMessage, message_id)
    if not m:
        return None, None
    return m, db.get(ChatConversation, m.conversation_id)


def _authorize_post(db, conv, user: dict) -> Optional[JSONResponse]:
    if conv.type == "dm":
        members = store.member_user_ids(db, conv.id)
        peer_id = next((m for m in members if m != user["user_id"]), None)
        if peer_id is None:
            return _err("CHAT_NOT_MEMBER", "Not a conversation member", 403)
        peer = db.query(User).filter(User.id == peer_id, User.deleted_at.is_(None)).first()
        peer_active = bool(peer and getattr(peer, "enable", 1))
        if not can_post_dm(peer_active=peer_active):
            return _err("CHAT_USER_INACTIVE", "Peer is inactive", 403)
        return None
    if conv.type == "team":
        rows = db.execute(
            text("SELECT user_id FROM team_members WHERE team_id = :tid"),
            {"tid": conv.team_id},
        ).all()
        is_member_local = any(r[0] == user["user_id"] for r in rows)
        if not can_post_team(role_name=user.get("role_name"), is_member=is_member_local):
            return _err("CHAT_TEAM_MEMBERSHIP_REQUIRED",
                        "Not a member of this team", 403)
        return None
    if conv.type == "general":
        store.ensure_general_member(db, user["user_id"])
        return None if can_post_general() else _err("CHAT_NOT_MEMBER",
                                                    "Cannot post in #general", 403)
    return _err("CHAT_NOT_FOUND", "Unknown conversation type", 404)


def _resolve_usernames(db, names: List[str]) -> List[int]:
    if not names:
        return []
    rows = db.execute(text(
        "SELECT id FROM users "
        "WHERE LOWER(username) IN :names "
        "  AND deleted_at IS NULL AND enable = 1"
    ).bindparams(bindparam("names", expanding=True)),
        {"names": names},
    ).all()
    return [r[0] for r in rows]


@router.post("/conversations/{conversation_id}/messages",
             response_model=MessageOut,
             responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def send_message(conversation_id: int, req: SendMessageRequest,
                 user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        conv = db.get(ChatConversation, conversation_id)
        if not conv or conv.deleted_at is not None:
            return _err("CHAT_NOT_FOUND", "Conversation not found", 404)

        err = _authorize_post(db, conv, user)
        if err:
            return err

        clean_body = sanitise_body(req.body) if req.body else None
        msg = store.create_message(
            db, conversation_id=conv.id, sender_id=user["user_id"],
            message_type=req.message_type, body=clean_body,
            attachment_id=req.attachment_id,
            reply_to_message_id=req.reply_to_message_id,
        )

        # Resolve mentions
        mention_user_ids: List[int] = []
        if msg.message_type == "text" and msg.body:
            usernames = extract_usernames(msg.body)
            mention_user_ids = _resolve_usernames(db, usernames)
            if mention_user_ids:
                store.add_mentions(db, msg.id, mention_user_ids)
                bridge.handle_mention_for_users(
                    db=db, message=msg, sender=user,
                    mentioned_user_ids=mention_user_ids,
                )

        att = _fetch_attachment(db, msg.attachment_id)
        message_payload = _to_message_out(msg, attachment=att,
                                          mention_ids=mention_user_ids, db=db)
        recipients = [m for m in store.member_user_ids(db, conv.id) if m != user["user_id"]]
        # Inbox preview row that mirrors what GET /chat/conversations would return
        sender_info = user_info_cache.get_user_info(msg.sender_id, db=db)
        preview = {
            "id": msg.id,
            "sender_id": msg.sender_id,
            "sender_username": sender_info.get("username"),
            "sender_name": sender_info.get("name"),
            "message_type": msg.message_type,
            "body_preview": store._preview_for(msg.message_type, msg.body, msg.deleted_at),
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "deleted_at": None,
        }
        delivered_now_at = datetime.utcnow().isoformat()
        for uid in recipients:
            redis_chat.publish_message_new(user_id=uid, message=message_payload,
                                           conversation_id=conv.id)
            # Bump the recipient's inbox row so the cell can be re-rendered
            unread = store.unread_count_for_user(db, conv.id, uid)
            redis_chat.publish_inbox_bump(user_id=uid, conversation_id=conv.id,
                                          latest_message=preview,
                                          unread_count=unread)
            # Two-tick logic: if the recipient currently has a chat WS open,
            # the message reached them. Mark delivered + tell the sender.
            if chat_ws_manager.is_online(uid):
                store.mark_delivered(db, message_id=msg.id, user_id=uid)
                redis_chat.publish_message_delivered(
                    user_id=user["user_id"],
                    message_id=msg.id,
                    recipient_user_id=uid,
                    delivered_at=delivered_now_at,
                )
        # Sender's own inbox cell update too (last_message_at / preview moves to top)
        redis_chat.publish_inbox_bump(user_id=user["user_id"],
                                      conversation_id=conv.id,
                                      latest_message=preview,
                                      unread_count=0)
        bridge.handle_message_for_offline_recipients(
            db=db, conversation_id=conv.id, message=msg,
            sender=user, recipients=recipients,
        )
        return message_payload
    finally:
        db.close()


@router.get("/conversations/{conversation_id}/messages",
            response_model=PaginatedMessages,
            responses={403: {"model": ErrorResponse}})
def list_messages(conversation_id: int, cursor: Optional[str] = None,
                  limit: int = 50, user: dict = Depends(current_user)):
    from app.chat_layer.models import ChatMessageDelivery, ChatMessageRead
    from sqlalchemy import select as _select
    db = SessionLocal()
    try:
        if not store.is_member(db, conversation_id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER", "Not a conversation member", 403)
        rows, has_more, next_cursor = store.list_messages(
            db, conversation_id=conversation_id, cursor=cursor,
            limit=min(max(limit, 1), 100),
        )

        # Batch-fetch delivery + read receipts + reactions so reload renders
        # the right ticks AND reaction chips. One query per kind, not N.
        msg_ids = [m.id for m in rows]
        delivered_by_msg: dict = {}
        read_by_msg: dict = {}
        reactions_by_msg: dict = {}
        if msg_ids:
            d_rows = db.execute(
                _select(ChatMessageDelivery.message_id, ChatMessageDelivery.user_id)
                .where(ChatMessageDelivery.message_id.in_(msg_ids))
            ).all()
            for mid, uid in d_rows:
                delivered_by_msg.setdefault(mid, []).append(uid)
            r_rows = db.execute(
                _select(ChatMessageRead.message_id, ChatMessageRead.user_id)
                .where(ChatMessageRead.message_id.in_(msg_ids))
            ).all()
            for mid, uid in r_rows:
                read_by_msg.setdefault(mid, []).append(uid)
            reactions_by_msg = store.list_reactions_for_messages(db, msg_ids)

        items = []
        for m in rows:
            att = _fetch_attachment(db, m.attachment_id) if m.attachment_id else None
            grouped = store.group_reactions_by_emoji(reactions_by_msg.get(m.id, []))
            items.append(_to_message_out(
                m, attachment=att, db=db,
                delivered_to=delivered_by_msg.get(m.id, []),
                read_by=read_by_msg.get(m.id, []),
                reactions=grouped,
            ))
        return PaginatedMessages(items=items, next_cursor=next_cursor,
                                 has_more=has_more).model_dump(mode="json")
    finally:
        db.close()


def _mark_one_read(db, msg, conv, user_id: int) -> None:
    """Mark a single message read for `user_id`. Caller must have already
    verified membership. Publishes per-message read events but NOT
    `unread.update` — the bulk caller fires that once per affected conv."""
    store.mark_read(db, message_id=msg.id, user_id=user_id)
    store.update_last_read(db, conversation_id=conv.id,
                           user_id=user_id, message_id=msg.id)
    now = datetime.utcnow().isoformat()
    if conv.type == "dm":
        redis_chat.publish_message_read(
            user_id=msg.sender_id, message_id=msg.id,
            reader_user_id=user_id, read_at=now,
        )
    else:
        rc = store.read_count(db, msg.id)
        for uid in store.member_user_ids(db, conv.id):
            redis_chat.publish_message_read_count(
                user_id=uid, message_id=msg.id, conversation_id=conv.id,
                read_count=rc,
            )


@router.post("/messages/{message_id}/read", status_code=status.HTTP_204_NO_CONTENT,
             responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def mark_message_read(message_id: int, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        msg, conv = _fetch_message_and_conv(db, message_id)
        if not msg or not conv:
            return _err("CHAT_NOT_FOUND", "Message not found", 404)
        if not store.is_member(db, conv.id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER", "Not a member", 403)
        _mark_one_read(db, msg, conv, user["user_id"])
        # Cross-tab unread sync: tell the reader's other connections to clear the badge
        unread = store.unread_count_for_user(db, conv.id, user["user_id"])
        redis_chat.publish_unread_update(
            user_id=user["user_id"], conversation_id=conv.id, unread_count=unread,
        )
        return Response(status_code=204)
    finally:
        db.close()


@router.post("/messages/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_messages_read_bulk(req: MarkReadBulkRequest,
                            user: dict = Depends(current_user)):
    """Mark up to 200 messages read in one call. Best-effort: messages the
    caller can't see (not a member, or non-existent ids) are silently
    skipped. Per-message `message.read` / `message.read_count` events fire
    over the WS exactly as if you'd hit the single-message endpoint N
    times; one `unread.update` is published per affected conversation at
    the end (instead of per message) to avoid badge flicker."""
    db = SessionLocal()
    try:
        affected_convs: set = set()
        for mid in req.message_ids:
            msg, conv = _fetch_message_and_conv(db, mid)
            if not msg or not conv:
                continue
            if not store.is_member(db, conv.id, user["user_id"]):
                continue
            _mark_one_read(db, msg, conv, user["user_id"])
            affected_convs.add(conv.id)
        for cid in affected_convs:
            unread = store.unread_count_for_user(db, cid, user["user_id"])
            redis_chat.publish_unread_update(
                user_id=user["user_id"], conversation_id=cid, unread_count=unread,
            )
        return Response(status_code=204)
    finally:
        db.close()


@router.patch("/messages/{message_id}", response_model=MessageOut,
              responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
                         410: {"model": ErrorResponse}})
def edit_message(message_id: int, req: EditMessageRequest,
                 user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        msg, conv = _fetch_message_and_conv(db, message_id)
        if not msg or not conv:
            return _err("CHAT_NOT_FOUND", "Message not found", 404)
        if msg.deleted_at is not None:
            return _err("CHAT_MESSAGE_DELETED", "Message deleted", 410)
        if msg.message_type != "text":
            return _err("CHAT_EDIT_NOT_OWNER", "Only text messages can be edited", 403)
        if msg.sender_id != user["user_id"]:
            return _err("CHAT_EDIT_NOT_OWNER", "Only sender can edit", 403)
        if not can_edit_message(sender_id=msg.sender_id, caller_id=user["user_id"],
                                created_at=msg.created_at):
            return _err("CHAT_EDIT_WINDOW_EXPIRED", "Edit window expired", 409)
        clean = sanitise_body(req.body)
        store.edit_message_body(db, message_id=msg.id, new_body=clean)
        db.refresh(msg)
        for uid in store.member_user_ids(db, conv.id):
            redis_chat.publish_message_edited(
                user_id=uid, message_id=msg.id, conversation_id=conv.id,
                body=msg.body, edited_at=msg.edited_at.isoformat() if msg.edited_at else "",
            )
        att = _fetch_attachment(db, msg.attachment_id)
        return _to_message_out(msg, attachment=att, db=db)
    finally:
        db.close()


@router.delete("/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT,
               responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def delete_message(message_id: int, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        msg, conv = _fetch_message_and_conv(db, message_id)
        if not msg or not conv:
            return _err("CHAT_NOT_FOUND", "Message not found", 404)
        if not can_delete_message(role_name=user.get("role_name")):
            return _err("CHAT_ADMIN_ONLY", "Only Admin/SuperAdmin can delete", 403)
        store.soft_delete_message(db, message_id=msg.id, deleted_by=user["user_id"])
        for uid in store.member_user_ids(db, conv.id):
            redis_chat.publish_message_deleted(
                user_id=uid, message_id=msg.id, conversation_id=conv.id,
                deleted_by=user["user_id"],
            )
        return Response(status_code=204)
    finally:
        db.close()


@router.post("/messages/{message_id}/forward", response_model=List[MessageOut],
             responses={403: {"model": ErrorResponse}, 410: {"model": ErrorResponse}})
def forward_message(message_id: int, req: ForwardMessageRequest,
                    user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        orig, _orig_conv = _fetch_message_and_conv(db, message_id)
        if not orig:
            return _err("CHAT_NOT_FOUND", "Source message not found", 404)
        if orig.deleted_at is not None:
            return _err("CHAT_MESSAGE_DELETED", "Cannot forward deleted message", 410)
        for cid in req.conversation_ids:
            if not store.is_member(db, cid, user["user_id"]):
                return _err("CHAT_FORWARD_NOT_MEMBER",
                            f"Not a member of conversation {cid}", 403)
        out_payloads = []
        # If the original was already forwarded, preserve the *true* origin
        # so chains of forwards always credit the first author, not the
        # intermediate hops.
        true_origin_sender_id = orig.forwarded_from_sender_id or orig.sender_id
        for cid in req.conversation_ids:
            new_msg = store.create_message(
                db, conversation_id=cid, sender_id=user["user_id"],
                message_type=orig.message_type, body=orig.body,
                attachment_id=orig.attachment_id,
                forwarded_from_message_id=orig.id,
                forwarded_from_sender_id=true_origin_sender_id,
            )
            att = _fetch_attachment(db, new_msg.attachment_id)
            payload = _to_message_out(new_msg, attachment=att, db=db)
            out_payloads.append(payload)
            delivered_at = datetime.utcnow().isoformat()
            for uid in store.member_user_ids(db, cid):
                if uid == user["user_id"]:
                    continue
                redis_chat.publish_message_new(user_id=uid, message=payload,
                                               conversation_id=cid)
                if chat_ws_manager.is_online(uid):
                    store.mark_delivered(db, message_id=new_msg.id, user_id=uid)
                    redis_chat.publish_message_delivered(
                        user_id=user["user_id"],
                        message_id=new_msg.id,
                        recipient_user_id=uid,
                        delivered_at=delivered_at,
                    )
        return out_payloads
    finally:
        db.close()


# ---------- Reactions ----------

@router.post("/messages/{message_id}/reactions",
             status_code=status.HTTP_204_NO_CONTENT,
             responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def add_reaction(message_id: int, req: AddReactionRequest,
                 user: dict = Depends(current_user)):
    """Add an emoji reaction to a message. Idempotent — re-adding the same
    emoji is a no-op (no error, no duplicate event). Fans out
    `message.reaction.added` to every conversation member."""
    db = SessionLocal()
    try:
        msg, conv = _fetch_message_and_conv(db, message_id)
        if not msg or not conv:
            return _err("CHAT_NOT_FOUND", "Message not found", 404)
        if not store.is_member(db, conv.id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER", "Not a member", 403)
        if msg.deleted_at is not None:
            return _err("CHAT_MESSAGE_DELETED",
                        "Cannot react to deleted message", 410)
        added = store.add_reaction(
            db, message_id=msg.id, user_id=user["user_id"], emoji=req.emoji,
        )
        if added:
            for uid in store.member_user_ids(db, conv.id):
                redis_chat.publish_reaction_added(
                    user_id=uid, message_id=msg.id, conversation_id=conv.id,
                    reactor_user_id=user["user_id"], emoji=req.emoji,
                )
        return Response(status_code=204)
    finally:
        db.close()


@router.delete("/messages/{message_id}/reactions",
               status_code=status.HTTP_204_NO_CONTENT,
               responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def remove_reaction(message_id: int, emoji: str,
                    user: dict = Depends(current_user)):
    """Remove your own emoji reaction from a message. `emoji` is a query
    string parameter (DELETEs traditionally don't carry a body). Idempotent."""
    db = SessionLocal()
    try:
        msg, conv = _fetch_message_and_conv(db, message_id)
        if not msg or not conv:
            return _err("CHAT_NOT_FOUND", "Message not found", 404)
        if not store.is_member(db, conv.id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER", "Not a member", 403)
        removed = store.remove_reaction(
            db, message_id=msg.id, user_id=user["user_id"], emoji=emoji,
        )
        if removed:
            for uid in store.member_user_ids(db, conv.id):
                redis_chat.publish_reaction_removed(
                    user_id=uid, message_id=msg.id, conversation_id=conv.id,
                    reactor_user_id=user["user_id"], emoji=emoji,
                )
        return Response(status_code=204)
    finally:
        db.close()
