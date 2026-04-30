"""POST /ai-chat/ask — kicks off an AI turn.

The HTTP response returns a `task_id` immediately. The actual turn runs in
a FastAPI background task; tokens stream out via the existing chat
WebSocket as `ai.token` events. The final composed message is posted
through `chat_store.create_message` so it appears in history identically
to any other DM message.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.ai_chat_layer import agent as ai_agent
from app.ai_chat_layer.api.dm import get_or_create_ai_dm
from app.ai_chat_layer.schemas import AskRequest, AskTaskAck
from app.chat_layer.auth import current_user
from app.database_Layer.db_config import SessionLocal
from app.notification_layer import redis_manager

logger = logging.getLogger("app_logger")

router = APIRouter()


def _publish_event(user_id: int, type_: str, data: Dict[str, Any]) -> None:
    """Publish an event onto the chat WS pub/sub channel for the given user.

    Mirrors the `redis_chat._publish` shape — type/data/timestamp wrapper —
    so the existing FE WebSocket handler picks it up without changes."""
    from datetime import datetime
    try:
        client = redis_manager.get_notification_redis()
        frame = {"type": type_, "data": data,
                 "timestamp": datetime.utcnow().isoformat() + "Z"}
        client.publish(f"chat:user:{user_id}", json.dumps(frame, default=str))
    except Exception as exc:
        logger.warning("ai event publish failed: %s", exc)


def _run_in_background(*, prompt: str, refs: List[Dict[str, Any]],
                       conversation_id: int, user: Dict[str, Any],
                       task_id: str, ip_address: str | None) -> None:
    """Run a single agent turn end-to-end in a worker thread.

    Token streaming: the agent calls our `stream_cb` for every text
    fragment it receives from Gemini; we forward each fragment as an
    `ai.token` event so the FE bubble grows live. Mid-stream entity /
    chart refs flow through `refs_cb` as `ai.refs` events so cards
    appear next to the running text instead of all at the end.
    """
    user_id = int(user.get("user_id"))
    db: Session = SessionLocal()

    def _on_delta(fragment: str) -> None:
        if not fragment:
            return
        _publish_event(user_id, "ai.token", {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "delta": fragment,
            "final": False,
        })

    def _on_refs(new_refs: List[Dict[str, Any]]) -> None:
        if not new_refs:
            return
        _publish_event(user_id, "ai.refs", {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "refs": new_refs,
        })

    def _on_status(label_or_event) -> None:
        # Backward compat: agent may send a plain string (legacy) OR a
        # dict with {phase, label, tools, tool_name}. We forward both
        # with a single `status` text field for legacy clients plus
        # the structured fields the new live-thinking indicator uses.
        if isinstance(label_or_event, dict):
            phase = label_or_event.get("phase")
            tool_name = label_or_event.get("tool_name")
            status_text = (label_or_event.get("label") or "").strip()
            # Allow empty-label heartbeats (e.g. tool_complete) through —
            # they still carry useful FE state in `phase` + `tool_name`.
            if not status_text and not phase:
                return
            payload = {
                "task_id": task_id,
                "conversation_id": conversation_id,
                "status": status_text,
                "phase": phase,
                "tools": label_or_event.get("tools") or [],
                "tool_name": tool_name,
            }
        else:
            label = (label_or_event or "").strip()
            if not label:
                return
            payload = {
                "task_id": task_id,
                "conversation_id": conversation_id,
                "status": label,
            }
        _publish_event(user_id, "ai.status", payload)

    try:
        _publish_event(user_id, "ai.start", {
            "task_id": task_id, "conversation_id": conversation_id,
        })
        result = ai_agent.run_turn(
            db=db, user=user, prompt=prompt, refs=refs,
            conversation_id=conversation_id, ip_address=ip_address,
            stream_cb=_on_delta,
            refs_cb=_on_refs,
            status_cb=_on_status,
        )
        _publish_event(user_id, "ai.token", {
            "task_id": task_id, "conversation_id": conversation_id,
            "delta": "",
            "final": True,
        })
        _publish_event(user_id, "ai.complete", {
            "task_id": task_id, "conversation_id": conversation_id,
            "message_id": result.get("message_id"),
            "trace": result.get("trace"),
            "tokens_in": result.get("tokens_in"),
            "tokens_out": result.get("tokens_out"),
        })
    except Exception as exc:
        logger.exception("ai background task failed")
        _publish_event(user_id, "ai.error", {
            "task_id": task_id, "conversation_id": conversation_id,
            "error": str(exc),
        })
    finally:
        db.close()


@router.post("/ask", response_model=AskTaskAck)
def ask(req: AskRequest, request: Request,
        background_tasks: BackgroundTasks,
        user: dict = Depends(current_user)) -> AskTaskAck:
    """Submit a prompt to the user's AI Assistant. Streams via WebSocket."""
    db: Session = SessionLocal()
    try:
        conversation_id = req.conversation_id
        if conversation_id is None:
            conversation_id, _ = get_or_create_ai_dm(db, int(user["user_id"]))
        # Validate conversation membership for safety.
        from app.chat_layer.store import is_member
        if not is_member(db, conversation_id, int(user["user_id"])):
            raise HTTPException(status_code=403,
                                detail="Not a member of this conversation")
    finally:
        db.close()

    task_id = uuid.uuid4().hex
    refs_json = [r.model_dump() for r in req.refs] if req.refs else []
    ip = (request.client.host if request and request.client else None)

    # Run the agent in a thread so the HTTP request returns immediately
    # while the WS streams progress. FastAPI BackgroundTasks block the
    # response otherwise (they run before sending the body).
    threading.Thread(
        target=_run_in_background,
        kwargs=dict(prompt=req.prompt, refs=refs_json,
                    conversation_id=conversation_id, user=user,
                    task_id=task_id, ip_address=ip),
        daemon=True,
    ).start()

    return AskTaskAck(task_id=task_id, conversation_id=conversation_id)


@router.get("/conversation")
def get_my_ai_conversation(user: dict = Depends(current_user)) -> Dict[str, int]:
    """Resolve the caller's AI Assistant DM conversation id (creates if needed)."""
    db: Session = SessionLocal()
    try:
        conv_id, bot_id = get_or_create_ai_dm(db, int(user["user_id"]))
        return {"conversation_id": conv_id, "ai_bot_user_id": bot_id}
    finally:
        db.close()
