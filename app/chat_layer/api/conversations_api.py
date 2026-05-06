"""Conversation endpoints."""
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

from app.chat_layer import presence as presence_helper, store
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import can_post_dm, can_post_team
from app.chat_layer.models import ChatConversation
from app.chat_layer.schemas import (
    ConversationOut, CreateDMRequest, ErrorResponse,
)
from app.database_Layer.db_config import SessionLocal
from app.database_Layer.db_model import User

router = APIRouter()


def _err(code: str, msg: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error_code": code, "message": msg})


def _is_user_active(db, user_id: int) -> bool:
    u = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    return bool(u and getattr(u, "enable", 1))


def _serialise(conv, members):
    return ConversationOut(
        id=conv.id, type=conv.type, team_id=conv.team_id, title=conv.title,
        last_message_at=conv.last_message_at, members=members,
    ).model_dump(mode="json")


def _fetch_team_member_ids(db, team_id: int) -> List[int]:
    rows = db.execute(
        text("SELECT user_id FROM team_members WHERE team_id = :tid"),
        {"tid": team_id},
    ).all()
    return [r[0] for r in rows]


@router.post("/conversations/dm", response_model=ConversationOut,
             responses={403: {"model": ErrorResponse}})
def create_dm(req: CreateDMRequest, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        if not _is_user_active(db, req.peer_user_id):
            return _err("CHAT_USER_INACTIVE", "Peer user is not active", 403)
        if not can_post_dm(peer_active=True):
            return _err("CHAT_USER_INACTIVE", "Cannot DM inactive user", 403)
        conv, newly_added = store.get_or_create_dm(db, user["user_id"], req.peer_user_id)
        members = store.member_user_ids(db, conv.id)
        # Both users now share this DM. Push current presence to both ends
        # so they see each other's online dot immediately — without this,
        # neither side gets a presence event until the next reconnect.
        if newly_added:
            presence_helper.announce_presence_to(
                db=db, target_user_ids=members, about_user_ids=members,
            )
        return _serialise(conv, members)
    finally:
        db.close()


@router.get("/conversations", response_model=List[ConversationOut])
def list_conversations(user: dict = Depends(current_user)):
    """WhatsApp-style inbox: all conversations the caller belongs to,
    enriched with latest message preview, unread count, and peer/team info.
    Sorted by last_message_at DESC NULLS LAST."""
    db = SessionLocal()
    try:
        store.ensure_general_member(db, user["user_id"])
        return store.inbox_for_user(db, user["user_id"])
    finally:
        db.close()


@router.get("/conversations/general", response_model=ConversationOut)
def get_general(user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        store.ensure_general_member(db, user["user_id"])
        conv = store.get_general_conversation(db)
        return _serialise(conv, store.member_user_ids(db, conv.id))
    finally:
        db.close()


@router.get("/conversations/team/{team_id}", response_model=ConversationOut,
            responses={403: {"model": ErrorResponse}})
def get_team_conversation(team_id: int, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        members = _fetch_team_member_ids(db, team_id)
        is_member_local = user["user_id"] in members
        if not can_post_team(role_name=user.get("role_name"), is_member=is_member_local):
            return _err("CHAT_TEAM_MEMBERSHIP_REQUIRED",
                        "You are not a member of this team", 403)
        conv, newly_added = store.get_or_create_team_conversation(
            db, team_id=team_id, member_user_ids=members,
            created_by=user["user_id"],
        )
        all_members = store.member_user_ids(db, conv.id)
        # Cross-announce only between newly-added members and existing ones.
        # On a brand-new team chat that's everyone × everyone (still small for
        # typical team sizes); on an established chat with one new member it's
        # 2*(N-1) events instead of N*N.
        if newly_added:
            existing = [uid for uid in all_members if uid not in newly_added]
            # Newly-added members learn about everyone (incl. each other).
            presence_helper.announce_presence_to(
                db=db, target_user_ids=newly_added, about_user_ids=all_members,
            )
            # Existing members learn about the new arrivals.
            if existing:
                presence_helper.announce_presence_to(
                    db=db, target_user_ids=existing, about_user_ids=newly_added,
                )
        return _serialise(conv, all_members)
    finally:
        db.close()


@router.get("/conversations/{conversation_id}", response_model=ConversationOut,
            responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def get_conversation(conversation_id: int, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        if not store.is_member(db, conversation_id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER", "Not a conversation member", 403)
        conv = db.get(ChatConversation, conversation_id)
        if not conv or conv.deleted_at is not None:
            return _err("CHAT_NOT_FOUND", "Conversation not found", 404)
        # Return the SAME enriched shape the inbox endpoint returns —
        # with peer info (DM), team info, latest_message preview, and
        # unread_count. Without this, the client's per-conversation cache
        # gets overwritten with a stripped-down record on every refetch
        # (header flickers from "Alice" → "Direct Message").
        enriched = store.inbox_row_for(db, user["user_id"], conv.id)
        if enriched is not None:
            return enriched
        # Fallback: caller is technically a member but the inbox query
        # didn't return a row (rare). Serve the basic shape.
        return _serialise(conv, store.member_user_ids(db, conv.id))
    finally:
        db.close()


# ── User lookup (used by the task-assignee picker) ───────────────────

class _UserLookupRequest(BaseModel):
    user_ids: List[int] = Field(..., min_length=1, max_length=200)


class _UserLookupOut(BaseModel):
    id: int
    name: Optional[str] = None
    username: Optional[str] = None


@router.post("/users/lookup", response_model=List[_UserLookupOut])
def lookup_users(body: "_UserLookupRequest",
                  user: dict = Depends(current_user)) -> List["_UserLookupOut"]:
    """Resolve a batch of user_ids to {id, name, username}. Used by
    the task composer's assignee picker (and any other place that
    needs to render a user list from a member_id array). Caller must
    be an authenticated user; visibility is intentionally lax — chat
    members already see each other's names through every other
    surface, and this endpoint never reveals emails / roles.
    """
    if not body.user_ids:
        return []
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT id, name, username FROM users "
                "WHERE id IN :ids AND deleted_at IS NULL",
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": list({int(i) for i in body.user_ids})},
        ).all()
        return [
            _UserLookupOut(
                id=int(r._mapping["id"]),
                name=r._mapping.get("name"),
                username=r._mapping.get("username"),
            )
            for r in rows
        ]
    finally:
        db.close()
