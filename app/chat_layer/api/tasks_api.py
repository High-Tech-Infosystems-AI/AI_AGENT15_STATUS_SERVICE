"""Tasks inside chats. Pairs a `chat_messages` row of
`message_type='task'` 1:1 with a `chat_tasks` row + assignee list.
Each assignee can mark themselves done independently; the parent
status (open / in_progress / done / cancelled) is the creator's
view of the task as a whole. The card payload (assignees, counts,
priority, due) ships in the resolved ref's `params` so TaskCard
renders without a follow-up fetch."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.chat_layer import (
    entity_resolver, notification_bridge as bridge, redis_chat, store,
    user_info_cache,
)
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import is_admin as _is_admin_role
from app.chat_layer.models import (
    ChatConversation, ChatMessage, ChatTask, ChatTaskAssignee,
)
from app.chat_layer.schemas import (
    ErrorResponse, MessageOut, TaskAssigneesUpdate, TaskCreate, TaskOut,
    TaskStatusUpdate,
)
from app.chat_layer.ws_manager import ws_manager as chat_ws_manager
from app.database_Layer.db_config import SessionLocal

logger = logging.getLogger("app_logger")

router = APIRouter()


def _err(code: str, msg: str, status: int):
    return JSONResponse(
        status_code=status, content={"error_code": code, "message": msg},
    )


def _broadcast_task(db: Session, *, conv_id: int, msg: ChatMessage,
                    sender_id: int, sender_user: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the task message into a MessageOut + push `message.new`
    to every member except the sender. Same shape as the regular send
    + forward paths."""
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


def _publish_task_state(db: Session, task_id: int) -> None:
    """Re-resolve a task and broadcast `chat.task_updated` so every
    member's TaskCard updates without polling."""
    task = db.get(ChatTask, task_id)
    if not task:
        return
    msg = db.get(ChatMessage, task.message_id)
    if not msg:
        return
    cards = entity_resolver.resolve(db, [{"type": "task", "id": task.id}])
    card = cards[0] if cards else None
    if not card:
        return
    for uid in store.member_user_ids(db, msg.conversation_id):
        redis_chat.publish_event(
            user_id=uid, event_type="chat.task_updated",
            data={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "task_id": task.id,
                "card": card,
            },
        )


def _can_modify(task: ChatTask, user: Dict[str, Any]) -> bool:
    """Creator + admins can mutate the task header / assignees /
    overall status. Assignees can flip their own status only."""
    return (
        task.created_by == user["user_id"]
        or _is_admin_role(user.get("role_name"))
    )


@router.post("/conversations/{conversation_id}/tasks",
             response_model=MessageOut,
             responses={403: {"model": ErrorResponse},
                        404: {"model": ErrorResponse}})
def create_task(conversation_id: int, body: TaskCreate,
                user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        conv = db.get(ChatConversation, conversation_id)
        if not conv:
            return _err("CHAT_NOT_FOUND", "Conversation not found", 404)
        if not store.is_member(db, conversation_id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER",
                        "Not a member of this conversation", 403)
        # Validate all assignees are members of this conversation.
        members = set(store.member_user_ids(db, conversation_id))
        bad = [uid for uid in body.assignee_ids if uid not in members]
        if bad:
            return _err(
                "CHAT_TASK_BAD_ASSIGNEE",
                f"Assignees not in conversation: {bad}", 400,
            )
        msg = store.create_message(
            db, conversation_id=conversation_id,
            sender_id=user["user_id"],
            message_type="task", body=body.title,
            refs=None,
        )
        task = ChatTask(
            message_id=msg.id, title=body.title,
            description=body.description,
            due_at=body.due_at, priority=body.priority,
            status="open", created_by=user["user_id"],
        )
        db.add(task)
        db.flush()
        for uid in body.assignee_ids:
            db.add(ChatTaskAssignee(
                task_id=task.id, user_id=uid,
                assigned_by=user["user_id"],
            ))
        msg.refs = [{"type": "task", "id": task.id}]
        db.commit()
        db.refresh(msg)
        return _broadcast_task(
            db, conv_id=conversation_id, msg=msg,
            sender_id=user["user_id"], sender_user=user,
        )
    finally:
        db.close()


def _task_card_to_out(card: Dict[str, Any], task_id: int) -> Dict[str, Any]:
    params = (card or {}).get("params") or {}
    return TaskOut.model_validate({**params, "id": task_id}).model_dump(mode="json")


@router.patch("/tasks/{task_id}/status", response_model=TaskOut,
              responses={403: {"model": ErrorResponse},
                         404: {"model": ErrorResponse}})
def update_task_status(task_id: int, body: TaskStatusUpdate,
                        user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        task = db.get(ChatTask, task_id)
        if not task:
            return _err("CHAT_NOT_FOUND", "Task not found", 404)
        if not _can_modify(task, user):
            return _err("CHAT_TASK_NOT_OWNER",
                        "Only the creator or an admin can change status", 403)
        task.status = body.status
        if body.status == "done":
            task.completed_at = datetime.utcnow()
            task.completed_by = user["user_id"]
        else:
            task.completed_at = None
            task.completed_by = None
        db.commit()
        _publish_task_state(db, task.id)
        cards = entity_resolver.resolve(db, [{"type": "task", "id": task.id}])
        return _task_card_to_out(cards[0] if cards else {}, task.id)
    finally:
        db.close()


@router.post("/tasks/{task_id}/mark-mine-done", response_model=TaskOut,
             responses={403: {"model": ErrorResponse},
                        404: {"model": ErrorResponse}})
def mark_my_assignee_done(task_id: int, user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        task = db.get(ChatTask, task_id)
        if not task:
            return _err("CHAT_NOT_FOUND", "Task not found", 404)
        row = db.query(ChatTaskAssignee).filter(
            ChatTaskAssignee.task_id == task.id,
            ChatTaskAssignee.user_id == user["user_id"],
        ).first()
        if not row:
            return _err("CHAT_TASK_NOT_ASSIGNED",
                        "You are not assigned to this task", 403)
        row.status = "done"
        row.completed_at = datetime.utcnow()
        # If every assignee is done, auto-flip the parent status.
        remaining_open = db.query(ChatTaskAssignee).filter(
            ChatTaskAssignee.task_id == task.id,
            ChatTaskAssignee.status != "done",
        ).count()
        if remaining_open == 0 and task.status not in ("done", "cancelled"):
            task.status = "done"
            task.completed_at = datetime.utcnow()
            task.completed_by = user["user_id"]
        db.commit()
        _publish_task_state(db, task.id)
        cards = entity_resolver.resolve(db, [{"type": "task", "id": task.id}])
        return _task_card_to_out(cards[0] if cards else {}, task.id)
    finally:
        db.close()


@router.post("/tasks/{task_id}/reopen-mine", response_model=TaskOut,
             responses={403: {"model": ErrorResponse},
                        404: {"model": ErrorResponse}})
def reopen_my_assignee(task_id: int, user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        task = db.get(ChatTask, task_id)
        if not task:
            return _err("CHAT_NOT_FOUND", "Task not found", 404)
        row = db.query(ChatTaskAssignee).filter(
            ChatTaskAssignee.task_id == task.id,
            ChatTaskAssignee.user_id == user["user_id"],
        ).first()
        if not row:
            return _err("CHAT_TASK_NOT_ASSIGNED",
                        "You are not assigned to this task", 403)
        row.status = "open"
        row.completed_at = None
        if task.status == "done":
            task.status = "in_progress"
            task.completed_at = None
            task.completed_by = None
        db.commit()
        _publish_task_state(db, task.id)
        cards = entity_resolver.resolve(db, [{"type": "task", "id": task.id}])
        return _task_card_to_out(cards[0] if cards else {}, task.id)
    finally:
        db.close()


@router.patch("/tasks/{task_id}/assignees", response_model=TaskOut,
              responses={403: {"model": ErrorResponse},
                         404: {"model": ErrorResponse}})
def update_task_assignees(task_id: int, body: TaskAssigneesUpdate,
                           user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        task = db.get(ChatTask, task_id)
        if not task:
            return _err("CHAT_NOT_FOUND", "Task not found", 404)
        if not _can_modify(task, user):
            return _err("CHAT_TASK_NOT_OWNER",
                        "Only the creator or an admin can edit assignees", 403)
        msg = db.get(ChatMessage, task.message_id)
        if msg:
            members = set(store.member_user_ids(db, msg.conversation_id))
            bad = [uid for uid in body.assignee_ids if uid not in members]
            if bad:
                return _err(
                    "CHAT_TASK_BAD_ASSIGNEE",
                    f"Assignees not in conversation: {bad}", 400,
                )
        existing = {
            a.user_id: a for a in db.query(ChatTaskAssignee).filter(
                ChatTaskAssignee.task_id == task.id,
            ).all()
        }
        wanted = set(body.assignee_ids)
        # Add newcomers.
        for uid in wanted - existing.keys():
            db.add(ChatTaskAssignee(
                task_id=task.id, user_id=uid, assigned_by=user["user_id"],
            ))
        # Remove dropped.
        for uid in existing.keys() - wanted:
            db.delete(existing[uid])
        db.commit()
        _publish_task_state(db, task.id)
        cards = entity_resolver.resolve(db, [{"type": "task", "id": task.id}])
        return _task_card_to_out(cards[0] if cards else {}, task.id)
    finally:
        db.close()


@router.get("/tasks/{task_id}", response_model=TaskOut,
            responses={404: {"model": ErrorResponse}})
def get_task(task_id: int, user: dict = Depends(current_user)):
    db: Session = SessionLocal()
    try:
        task = db.get(ChatTask, task_id)
        if not task:
            return _err("CHAT_NOT_FOUND", "Task not found", 404)
        msg = db.get(ChatMessage, task.message_id)
        if msg and not store.is_member(db, msg.conversation_id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER",
                        "Not a member of this conversation", 403)
        cards = entity_resolver.resolve(db, [{"type": "task", "id": task.id}])
        return _task_card_to_out(cards[0] if cards else {}, task.id)
    finally:
        db.close()
