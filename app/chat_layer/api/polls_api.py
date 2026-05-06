"""Polls inside chats. A poll is a `chat_messages` row of
`message_type='poll'` paired 1:1 with a `chat_polls` row that holds
the question / options / votes. Voting / closing happens through
this router; the underlying chat message is broadcast with refs that
carry the live `params` payload (option counts, voted_by_me) so the
PollCard FE component renders without an extra fetch.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.chat_layer import (
    entity_resolver, notification_bridge as bridge, redis_chat,
    s3_chat_service as s3,  # noqa: F401  (kept for symmetry / future use)
    store, user_info_cache,
)
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import is_admin as _is_admin_role
from app.chat_layer.models import (
    ChatMessage, ChatPoll, ChatPollOption, ChatPollVote,
)
from app.chat_layer.schemas import (
    ErrorResponse, MessageOut, PollCreate, PollOut, PollVoteRequest,
)
from app.chat_layer.ws_manager import chat_ws_manager
from app.database_Layer.db_config import SessionLocal

logger = logging.getLogger("app_logger")

router = APIRouter()


def _err(code: str, msg: str, status: int):
    return JSONResponse(
        status_code=status, content={"error_code": code, "message": msg},
    )


def _broadcast_poll(db: Session, *, conv_id: int, msg: ChatMessage,
                    sender_id: int, sender_user: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the freshly-built poll message into a MessageOut (cards
    populated via entity_resolver) and broadcast `message.new` to every
    member except the sender. Returns the payload for the REST reply."""
    from app.chat_layer.api.messages_api import (
        _fetch_attachment, _to_message_out,
    )
    att = _fetch_attachment(db, msg.attachment_id)
    payload = _to_message_out(
        msg, attachment=att, db=db, caller_user_id=sender_id,
    )
    sender_info = user_info_cache.get_user_info(sender_id, db=db)
    preview = {
        "id": msg.id,
        "sender_id": sender_id,
        "sender_username": sender_info.get("username"),
        "sender_name": sender_info.get("name"),
        "message_type": msg.message_type,
        "body_preview": store._preview_for(
            msg.message_type, msg.body, msg.deleted_at,
        ),
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "deleted_at": None,
    }
    for uid in store.member_user_ids(db, conv_id):
        if uid == sender_id:
            continue
        redis_chat.publish_message_new(
            user_id=uid, message=payload, conversation_id=conv_id,
        )
        unread = store.unread_count_for_user(db, conv_id, uid)
        redis_chat.publish_inbox_bump(
            user_id=uid, conversation_id=conv_id,
            latest_message=preview, unread_count=unread,
        )
        if chat_ws_manager.is_online(uid):
            store.mark_delivered(db, message_id=msg.id, user_id=uid)
    redis_chat.publish_inbox_bump(
        user_id=sender_id, conversation_id=conv_id,
        latest_message=preview, unread_count=0,
    )
    bridge.handle_message_for_recipients(
        db=db, conversation_id=conv_id, message=msg, sender=sender_user,
        recipients=[u for u in store.member_user_ids(db, conv_id) if u != sender_id],
    )
    return payload


def _publish_poll_state(db: Session, poll_id: int) -> None:
    """Re-resolve a poll and push the updated card to every member of
    the parent conversation as a `chat.poll_updated` event so live
    vote bars don't need a refetch."""
    poll = db.get(ChatPoll, poll_id)
    if not poll:
        return
    msg = db.get(ChatMessage, poll.message_id)
    if not msg:
        return
    cards = entity_resolver.resolve(db, [{"type": "poll", "id": poll.id}])
    card = cards[0] if cards else None
    if not card:
        return
    for uid in store.member_user_ids(db, msg.conversation_id):
        redis_chat.publish_event(
            user_id=uid, event_type="chat.poll_updated",
            data={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "poll_id": poll.id,
                "card": card,
            },
        )


@router.post("/conversations/{conversation_id}/polls",
             response_model=MessageOut,
             responses={403: {"model": ErrorResponse},
                        404: {"model": ErrorResponse}})
def create_poll(conversation_id: int, body: PollCreate,
                user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        conv = store.get_conversation(db, conversation_id)
        if not conv:
            return _err("CHAT_NOT_FOUND", "Conversation not found", 404)
        if not store.is_member(db, conversation_id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER",
                        "Not a member of this conversation", 403)
        # Polls only make sense in group chats — DM polls aren't disallowed
        # but the FE hides the button there. Server-side we just allow it.
        msg = store.create_message(
            db, conversation_id=conversation_id,
            sender_id=user["user_id"],
            message_type="poll", body=body.question,
            refs=None,
        )
        poll = ChatPoll(
            message_id=msg.id, question=body.question,
            allow_multiple=1 if body.allow_multiple else 0,
            created_by=user["user_id"],
        )
        db.add(poll)
        db.flush()
        for idx, opt in enumerate(body.options):
            db.add(ChatPollOption(
                poll_id=poll.id, text=opt.text, position=idx,
            ))
        # Stamp the poll ref onto the message so it round-trips through
        # `_resolve_refs_for` and lands as a card in MessageOut.refs.
        msg.refs = [{"type": "poll", "id": poll.id}]
        db.commit()
        db.refresh(msg)
        return _broadcast_poll(
            db, conv_id=conversation_id, msg=msg,
            sender_id=user["user_id"], sender_user=user,
        )
    finally:
        db.close()


@router.post("/polls/{poll_id}/vote", response_model=PollOut,
             responses={403: {"model": ErrorResponse},
                        404: {"model": ErrorResponse},
                        410: {"model": ErrorResponse}})
def vote_on_poll(poll_id: int, body: PollVoteRequest,
                  user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        poll = db.get(ChatPoll, poll_id)
        if not poll:
            return _err("CHAT_NOT_FOUND", "Poll not found", 404)
        if poll.closed_at is not None:
            return _err("CHAT_POLL_CLOSED",
                        "This poll is closed", 410)
        msg = db.get(ChatMessage, poll.message_id)
        if not msg:
            return _err("CHAT_NOT_FOUND", "Poll message missing", 404)
        if not store.is_member(db, msg.conversation_id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER",
                        "Not a member of this conversation", 403)
        # Validate option ids belong to this poll.
        valid_opts = db.query(ChatPollOption).filter(
            ChatPollOption.poll_id == poll.id,
            ChatPollOption.id.in_(body.option_ids),
        ).all()
        valid_ids = [o.id for o in valid_opts]
        if not valid_ids:
            return _err("CHAT_POLL_BAD_OPTION",
                        "No valid option ids supplied", 400)
        if not poll.allow_multiple and len(valid_ids) > 1:
            return _err("CHAT_POLL_SINGLE_CHOICE",
                        "This poll only allows one option", 400)
        # Replace prior votes for this user — change-of-mind support.
        db.query(ChatPollVote).filter(
            ChatPollVote.poll_id == poll.id,
            ChatPollVote.user_id == user["user_id"],
        ).delete(synchronize_session=False)
        for opt_id in valid_ids:
            db.add(ChatPollVote(
                poll_id=poll.id, option_id=opt_id,
                user_id=user["user_id"],
            ))
        db.commit()
        _publish_poll_state(db, poll.id)
        cards = entity_resolver.resolve(db, [{"type": "poll", "id": poll.id}])
        params = (cards[0] or {}).get("params") or {}
        return PollOut.model_validate({
            **params,
            # Re-key fields the schema expects.
            "id": poll.id,
        }).model_dump(mode="json")
    finally:
        db.close()


@router.post("/polls/{poll_id}/close", response_model=PollOut,
             responses={403: {"model": ErrorResponse},
                        404: {"model": ErrorResponse}})
def close_poll(poll_id: int, user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        poll = db.get(ChatPoll, poll_id)
        if not poll:
            return _err("CHAT_NOT_FOUND", "Poll not found", 404)
        if poll.closed_at is not None:
            # Idempotent — return the current state.
            pass
        elif poll.created_by != user["user_id"] and not _is_admin_role(
            user.get("role_name"),
        ):
            return _err("CHAT_POLL_NOT_OWNER",
                        "Only the poll creator or an admin can close", 403)
        else:
            poll.closed_at = datetime.utcnow()
            poll.closed_by = user["user_id"]
            db.commit()
            _publish_poll_state(db, poll.id)
        cards = entity_resolver.resolve(db, [{"type": "poll", "id": poll.id}])
        params = (cards[0] or {}).get("params") or {}
        return PollOut.model_validate({**params, "id": poll.id}).model_dump(mode="json")
    finally:
        db.close()


@router.get("/polls/{poll_id}", response_model=PollOut,
            responses={404: {"model": ErrorResponse}})
def get_poll(poll_id: int, user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        poll = db.get(ChatPoll, poll_id)
        if not poll:
            return _err("CHAT_NOT_FOUND", "Poll not found", 404)
        msg = db.get(ChatMessage, poll.message_id)
        if msg and not store.is_member(db, msg.conversation_id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER",
                        "Not a member of this conversation", 403)
        entity_resolver.set_caller(user["user_id"])
        try:
            cards = entity_resolver.resolve(db, [{"type": "poll", "id": poll.id}])
        finally:
            entity_resolver.set_caller(None)
        params = (cards[0] or {}).get("params") or {}
        return PollOut.model_validate({**params, "id": poll.id}).model_dump(mode="json")
    finally:
        db.close()
