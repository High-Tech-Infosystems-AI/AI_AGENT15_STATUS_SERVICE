"""Conversation endpoints."""
from typing import List

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.chat_layer import store
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
        conv = store.get_or_create_dm(db, user["user_id"], req.peer_user_id)
        members = store.member_user_ids(db, conv.id)
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
        conv = store.get_or_create_team_conversation(
            db, team_id=team_id, member_user_ids=members,
            created_by=user["user_id"],
        )
        return _serialise(conv, store.member_user_ids(db, conv.id))
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
        return _serialise(conv, store.member_user_ids(db, conv.id))
    finally:
        db.close()
