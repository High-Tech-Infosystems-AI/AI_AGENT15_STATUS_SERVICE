"""Append-only audit log of every AI query.

Mirrors the AuditLog pattern from AI_AGENT3_Resume_Analyzer/api/dependencies/audit_utils.py
but specialized for AI Q&A: prompt, tools called, model, token counts.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.ai_chat_layer.models import AiQueryAudit

logger = logging.getLogger("app_logger")


def log_query(
    db: Session,
    *,
    user_id: int,
    prompt: str,
    model: str,
    prompt_version: str,
    status: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    conversation_id: Optional[int] = None,
    refs: Optional[List[Dict[str, Any]]] = None,
    tools_called: Optional[List[Dict[str, Any]]] = None,
    error_msg: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Optional[int]:
    """Insert one audit row. Swallows DB errors to avoid breaking the
    request path — audit is best-effort, not load-bearing."""
    try:
        row = AiQueryAudit(
            user_id=user_id,
            conversation_id=conversation_id,
            prompt=(prompt or "")[:65535],
            refs=refs,
            tools_called=tools_called,
            model=(model or "unknown")[:64],
            prompt_version=(prompt_version or "")[:32],
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            latency_ms=int(latency_ms or 0),
            status=status,
            error_msg=(error_msg or None) and error_msg[:500],
            ip_address=(ip_address or None) and ip_address[:64],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    except Exception as exc:
        logger.warning("audit insert failed: %s", exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def list_recent(
    db: Session,
    *,
    user_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
) -> Iterable[AiQueryAudit]:
    q = db.query(AiQueryAudit)
    if user_id is not None:
        q = q.filter(AiQueryAudit.user_id == user_id)
    q = q.order_by(AiQueryAudit.created_at.desc())
    return q.offset(offset).limit(limit).all()


def count(db: Session, *, user_id: Optional[int] = None) -> int:
    q = db.query(AiQueryAudit)
    if user_id is not None:
        q = q.filter(AiQueryAudit.user_id == user_id)
    return q.count()
