"""Message endpoints: send, list, mark-read, edit, delete, forward."""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import bindparam, text

import app.chat_layer.notification_bridge as bridge
from app.chat_layer import (
    entity_resolver, redis_chat, s3_chat_service as s3, status_bot, store,
    user_info_cache,
)
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


def _resolve_refs_for(msg, db) -> list[dict]:
    """Resolve any structured `(type, id)` refs persisted on the message
    into full cards for the response. Cheap when the message has no refs.

    Synthetic AI ref types (`ai_artifact`, `ai_elicitation`) bypass the
    standard entity resolver — they carry their renderable metadata under
    `params` and have dedicated FE components. We pass them through
    unmolested so they reach the FE; otherwise the chart PNGs and the
    elicitation forms produced by the AI agent get silently dropped.
    """
    raw = getattr(msg, "refs", None) or []
    if not raw:
        return []
    cards = entity_resolver.resolve(db, raw)
    out: list[dict] = []
    for original, card in zip(raw, cards):
        if card:
            out.append(card)
            continue
        if not isinstance(original, dict):
            continue
        rtype = (original.get("type") or "")
        if rtype.startswith("ai_"):
            params = original.get("params") or {}
            out.append({
                "type": rtype,
                "id": original.get("id"),
                "title": (
                    params.get("title")
                    or original.get("title")
                    or None
                ),
                "deep_link": original.get("deep_link") or None,
                "fields": [],
                "params": params,
            })
            continue
        # Non-AI type whose resolver returned None — pass the raw
        # ref through so the FE still has (type, id) to work with
        # (e.g. PollCard / TaskCard can lazy-fetch via GET).
        # Without this, polls / tasks where the resolver hiccups
        # would render as plain text bubbles.
        if rtype in {"poll", "task"} and original.get("id") is not None:
            out.append({
                "type": rtype,
                "id": original.get("id"),
                "title": original.get("title") or None,
                "deep_link": None,
                "fields": [],
                "params": original.get("params") or None,
            })
    return out


def _to_message_out(msg, attachment=None, mention_ids=None, db=None,
                    read_by=None, delivered_to=None, reactions=None,
                    caller_user_id: Optional[int] = None) -> dict:
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
    # Tell the resolver who's asking — poll cards need this to compute
    # `voted_by_me`. Reset to None afterwards so a stale value can't
    # bleed across requests.
    if caller_user_id is not None:
        entity_resolver.set_caller(caller_user_id)
    refs_out = _resolve_refs_for(msg, db) if msg.deleted_at is None else []
    entity_resolver.set_caller(None)
    return MessageOut(
        id=msg.id, conversation_id=msg.conversation_id, sender_id=msg.sender_id,
        sender_username=sender_info.get("username"),
        sender_name=sender_info.get("name"),
        is_system=bool(getattr(msg, "is_system", 0)),
        message_type=msg.message_type, body=body_out, attachment=att_out,
        refs=refs_out,
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


# Special tokens that expand to "every member of this conversation"
# (excluding the sender — you don't mention yourself).
_EVERYONE_TOKENS = {"everyone", "all", "channel", "here"}


def _resolve_mentions_for_send(
    db, *, body: str, conversation_id: int, sender_user_id: int,
) -> List[int]:
    """Parse `@usernames` out of `body` and resolve them to user ids.
    Treats `@everyone` (and aliases `@all`, `@channel`, `@here`) as
    "every member of the conversation, except the sender".
    """
    usernames = [u.lower() for u in extract_usernames(body or "")]
    if not usernames:
        return []

    # Split into the special "everyone" group + literal username matches.
    everyone_requested = any(u in _EVERYONE_TOKENS for u in usernames)
    literals = [u for u in usernames if u not in _EVERYONE_TOKENS]

    resolved: set = set()
    if everyone_requested:
        for uid in store.member_user_ids(db, conversation_id):
            if uid != sender_user_id:
                resolved.add(uid)
    if literals:
        for uid in _resolve_usernames(db, literals):
            if uid != sender_user_id:
                resolved.add(uid)
    return sorted(resolved)


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
        # Validate refs against allowed types and shape early so a bad payload
        # never reaches the DB. Preserve `params` — for `report` refs that's
        # the picker-confirmed filter set (date_from / date_to / company_id /
        # …) the dashboard renderer reads to draw the right snapshot.
        refs_payload: list[dict] = []
        for r in (req.refs or []):
            r_dict = r.model_dump() if hasattr(r, "model_dump") else dict(r)
            if r_dict.get("type") not in entity_resolver.ENTITY_TYPES:
                continue
            entry: dict = {"type": r_dict["type"], "id": r_dict["id"]}
            params = r_dict.get("params")
            if isinstance(params, dict) and params:
                entry["params"] = params
            refs_payload.append(entry)

        # DM rule: only Admin / SuperAdmin may attach entity references in
        # a direct message. Picker UI hides the affordance for non-admins,
        # but enforce server-side too so a crafted request can't bypass.
        if refs_payload and conv.type == "dm":
            if not entity_resolver.is_admin_role(user.get("role_name")):
                return _err(
                    "CHAT_ENTITY_DM_FORBIDDEN",
                    "Entity references in direct messages are restricted to admins.",
                    403,
                )
        msg = store.create_message(
            db, conversation_id=conv.id, sender_id=user["user_id"],
            message_type=req.message_type, body=clean_body,
            attachment_id=req.attachment_id,
            reply_to_message_id=req.reply_to_message_id,
            refs=refs_payload or None,
        )

        # Resolve mentions — supports literal `@username` and `@everyone`.
        mention_user_ids: List[int] = []
        if msg.message_type == "text" and msg.body:
            mention_user_ids = _resolve_mentions_for_send(
                db, body=msg.body, conversation_id=conv.id,
                sender_user_id=user["user_id"],
            )
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
        delivered_to_now: List[int] = []
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
                delivered_to_now.append(uid)

        # Reflect the just-marked deliveries in the REST response. Without
        # this, the `message.delivered` WS event can race the REST reply
        # — if it arrives first, the client tries to update a message that
        # isn't in its cache yet and the update is silently dropped, leaving
        # the sender's tick stuck on ✓ (single) instead of advancing to
        # ✓✓ (delivered).
        if delivered_to_now:
            existing_d = list(message_payload.get("delivered_to") or [])
            merged_d = sorted({*existing_d, *delivered_to_now})
            message_payload["delivered_to"] = merged_d
            message_payload["delivered_count"] = len(merged_d)
        # Sender's own inbox cell update too (last_message_at / preview moves to top)
        redis_chat.publish_inbox_bump(user_id=user["user_id"],
                                      conversation_id=conv.id,
                                      latest_message=preview,
                                      unread_count=0)
        bridge.handle_message_for_recipients(
            db=db, conversation_id=conv.id, message=msg,
            sender=user, recipients=recipients,
        )

        # ─── /status command: post a Status Bot reply with fresh cards ──
        # Triggered when the user's body starts with "/status" AND the
        # message either carries refs[] OR has @@ref:type:id@@ tokens
        # inline. The bot's message is itself a regular chat row owned by
        # the bot user, with `is_system=1` so the FE can style it.
        try:
            _maybe_post_status_bot_reply(
                db=db, conv=conv, user=user, original_msg=msg,
                original_refs=refs_payload, preview_helper=preview,
            )
        except Exception as e:
            logger.warning("status bot reply failed: %s", e)

        return message_payload
    finally:
        db.close()


def _maybe_post_status_bot_reply(*, db, conv, user, original_msg,
                                 original_refs: list[dict],
                                 preview_helper: dict) -> None:
    """If the user just sent a `/status …@@ref…@@` message, persist a bot
    reply containing freshly-resolved cards for those refs. Fans out the
    reply on WS the same way a normal message does."""
    body = (original_msg.body or "").strip()
    inline = status_bot.find_status_command(body)
    if inline is None and not (body.lower().startswith("/status") and original_refs):
        return

    refs_to_resolve: list[dict] = []
    if inline:
        for t, rid in inline:
            try:
                refs_to_resolve.append({"type": t, "id": int(rid) if rid.isdigit() else rid})
            except Exception:
                refs_to_resolve.append({"type": t, "id": rid})
    refs_to_resolve.extend(original_refs or [])

    cards = entity_resolver.resolve(db, refs_to_resolve)
    if not any(cards):
        return

    bot_user_id = status_bot.ensure_status_bot_user(db)
    bot_body = " ".join(f"@@ref:{r['type']}:{r['id']}@@"
                        for r, c in zip(refs_to_resolve, cards) if c)
    bot_msg = store.create_message(
        db, conversation_id=conv.id, sender_id=bot_user_id,
        message_type="text", body=bot_body or "Status",
        refs=[{"type": r["type"], "id": r["id"]}
              for r, c in zip(refs_to_resolve, cards) if c],
        is_system=True,
    )
    bot_payload = _to_message_out(bot_msg, db=db)
    members = store.member_user_ids(db, conv.id)
    bot_preview = {
        "id": bot_msg.id,
        "sender_id": bot_user_id,
        "sender_username": "status_bot",
        "sender_name": "Status Bot",
        "message_type": "text",
        "body_preview": "Status update",
        "created_at": bot_msg.created_at.isoformat() if bot_msg.created_at else None,
        "deleted_at": None,
    }
    for uid in members:
        redis_chat.publish_message_new(user_id=uid, message=bot_payload,
                                       conversation_id=conv.id)
        unread = store.unread_count_for_user(db, conv.id, uid)
        redis_chat.publish_inbox_bump(user_id=uid, conversation_id=conv.id,
                                      latest_message=bot_preview,
                                      unread_count=unread if uid != user["user_id"] else 0)


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
        members = store.member_user_ids(db, conv.id)
        for uid in members:
            redis_chat.publish_message_edited(
                user_id=uid, message_id=msg.id, conversation_id=conv.id,
                body=msg.body, edited_at=msg.edited_at.isoformat() if msg.edited_at else "",
            )
        bridge.handle_message_edited(db=db, message=msg, sender=user,
                                     recipients=members)
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
        members = store.member_user_ids(db, conv.id)
        for uid in members:
            redis_chat.publish_message_deleted(
                user_id=uid, message_id=msg.id, conversation_id=conv.id,
                deleted_by=user["user_id"],
            )
        bridge.handle_message_deleted(db=db, message=msg, sender=user,
                                      recipients=members)
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
        # Image / voice / file forwards CLONE the attachment row instead of
        # sharing the original's `attachment_id`. Cloning gives the
        # forwarded message its own independent row (same underlying S3
        # object — no copy, just one extra row pointing at the same
        # `s3_key`). Benefits:
        #   * `uploaded_by` reflects the forwarder, so any future
        #     ownership / quota check works for the new message;
        #   * deleting / soft-deleting the original message's attachment
        #     never strands the forwarded copy;
        #   * downstream code can safely add a UNIQUE constraint on
        #     `chat_messages.attachment_id` without breaking forwards.
        # The clone happens once per forward (not per destination) so
        # multiple-target forwards still only cost one extra row per
        # destination, not one round-trip to S3.
        def _clone_attachment(att_id: Optional[int]) -> Optional[int]:
            if att_id is None:
                return None
            src = db.get(ChatMessageAttachment, att_id)
            if src is None:
                return None
            clone = ChatMessageAttachment(
                s3_key=src.s3_key,
                mime_type=src.mime_type,
                file_name=src.file_name,
                size_bytes=src.size_bytes,
                duration_seconds=src.duration_seconds,
                waveform_json=src.waveform_json,
                thumbnail_s3_key=src.thumbnail_s3_key,
                uploaded_by=user["user_id"],
            )
            db.add(clone)
            db.flush()
            return clone.id

        for cid in req.conversation_ids:
            cloned_attachment_id = _clone_attachment(orig.attachment_id)
            new_msg = store.create_message(
                db, conversation_id=cid, sender_id=user["user_id"],
                message_type=orig.message_type, body=orig.body,
                attachment_id=cloned_attachment_id,
                forwarded_from_message_id=orig.id,
                forwarded_from_sender_id=true_origin_sender_id,
            )
            att = _fetch_attachment(db, new_msg.attachment_id)
            payload = _to_message_out(new_msg, attachment=att, db=db)
            out_payloads.append(payload)
            delivered_at = datetime.utcnow().isoformat()
            delivered_to_now: List[int] = []
            sender_info = user_info_cache.get_user_info(user["user_id"], db=db)
            preview = {
                "id": new_msg.id,
                "sender_id": new_msg.sender_id,
                "sender_username": sender_info.get("username"),
                "sender_name": sender_info.get("name"),
                "message_type": new_msg.message_type,
                "body_preview": store._preview_for(
                    new_msg.message_type, new_msg.body, new_msg.deleted_at,
                ),
                "created_at": new_msg.created_at.isoformat()
                if new_msg.created_at
                else None,
                "deleted_at": None,
            }
            for uid in store.member_user_ids(db, cid):
                if uid == user["user_id"]:
                    continue
                redis_chat.publish_message_new(user_id=uid, message=payload,
                                               conversation_id=cid)
                # Bump the recipient's inbox preview so the destination
                # conversation pops to the top + shows the forwarded
                # body / "[image]" / "[file]" preview.
                unread = store.unread_count_for_user(db, cid, uid)
                redis_chat.publish_inbox_bump(
                    user_id=uid, conversation_id=cid,
                    latest_message=preview, unread_count=unread,
                )
                if chat_ws_manager.is_online(uid):
                    store.mark_delivered(db, message_id=new_msg.id, user_id=uid)
                    redis_chat.publish_message_delivered(
                        user_id=user["user_id"],
                        message_id=new_msg.id,
                        recipient_user_id=uid,
                        delivered_at=delivered_at,
                    )
                    delivered_to_now.append(uid)
            # Patch the per-destination response so the sender sees the
            # ✓✓ (delivered) tick immediately, regardless of WS-vs-REST
            # arrival order.
            if delivered_to_now:
                existing_d = list(payload.get("delivered_to") or [])
                merged_d = sorted({*existing_d, *delivered_to_now})
                payload["delivered_to"] = merged_d
                payload["delivered_count"] = len(merged_d)
            # Sender's own inbox cell update too — same pattern as the
            # regular send path. Without this the destination chat
            # doesn't bubble to the top of the forwarder's chat list,
            # making the forward feel like it didn't happen even
            # though the message landed correctly.
            redis_chat.publish_inbox_bump(
                user_id=user["user_id"], conversation_id=cid,
                latest_message=preview, unread_count=0,
            )
            recipients_for_notif = [u for u in store.member_user_ids(db, cid)
                                    if u != user["user_id"]]
            bridge.handle_message_forwarded(
                db=db, conversation_id=cid, message=new_msg,
                sender=user, recipients=recipients_for_notif,
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
