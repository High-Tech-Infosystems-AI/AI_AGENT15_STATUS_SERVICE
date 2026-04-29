"""Audit log endpoints.

Non-admin users see only their own queries; Admin/SuperAdmin can read the
whole tenant. Pagination is offset-based (small tables, simple UI)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.ai_chat_layer import audit
from app.ai_chat_layer.schemas import AuditOut, AuditPage
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import is_admin
from app.database_Layer.db_config import get_db

router = APIRouter(prefix="/audit")


@router.get("", response_model=AuditPage)
def list_audit(
    user: dict = Depends(current_user),
    db: Session = Depends(get_db),
    user_id: int | None = Query(default=None,
                                description="Admin only — filter to a specific user"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AuditPage:
    caller_admin = is_admin(user.get("role_name"))
    target_uid = None
    if not caller_admin:
        target_uid = int(user["user_id"])
    elif user_id is not None:
        target_uid = int(user_id)

    rows = audit.list_recent(db, user_id=target_uid, limit=limit, offset=offset)
    total = audit.count(db, user_id=target_uid)
    items = [AuditOut(
        id=r.id, user_id=r.user_id, conversation_id=r.conversation_id,
        prompt=r.prompt, refs=r.refs, tools_called=r.tools_called,
        model=r.model, prompt_version=r.prompt_version,
        tokens_in=r.tokens_in, tokens_out=r.tokens_out,
        latency_ms=r.latency_ms, status=r.status,
        error_msg=r.error_msg, created_at=r.created_at,
    ) for r in rows]
    next_offset = offset + len(items) if (offset + len(items)) < total else None
    return AuditPage(items=items, total=total, next_offset=next_offset)
