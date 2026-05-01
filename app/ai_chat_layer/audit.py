"""Append-only audit log of every AI query.

Mirrors the AuditLog pattern from AI_AGENT3_Resume_Analyzer/api/dependencies/audit_utils.py
but specialized for AI Q&A: prompt, tools called, model, token counts.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.ai_chat_layer.models import AiQueryAudit

logger = logging.getLogger("app_logger")


def _to_json_safe(value: Any, _depth: int = 0) -> Any:
    """Recursively coerce a structure into JSON-serializable types.

    The agent's `tools_called` trace can contain Pydantic models inside
    `args` (e.g. SuggestionItem / PdfSection / AdhocSeries) because
    LangChain instantiates the Pydantic args_schema before invoking
    `_runner`. SQLAlchemy's default JSON encoder doesn't know about
    those, so the audit insert blows up with `TypeError: Object of
    type SuggestionItem is not JSON serializable`. This walker turns
    Pydantic models into dicts, datetimes into ISO strings, decimals
    into floats, and falls back to `str(...)` for anything else.

    Bounded recursion (`_depth <= 6`) so a pathological circular
    structure can't lock up the audit path.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _depth > 6:
        return str(value)[:500]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        try:
            return float(value)
        except Exception:
            return str(value)
    # Pydantic v2 models expose `.model_dump()`; v1 exposes `.dict()`.
    dump = getattr(value, "model_dump", None) or getattr(value, "dict", None)
    if callable(dump):
        try:
            return _to_json_safe(dump(), _depth + 1)
        except Exception:
            return str(value)[:500]
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v, _depth + 1) for v in value]
    return str(value)[:500]


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
        # Coerce both JSON columns to plain primitives so SQLAlchemy's
        # default json.dumps doesn't choke on Pydantic models, datetimes,
        # or Decimals nested inside tool-call args.
        safe_refs = _to_json_safe(refs) if refs is not None else None
        safe_tools = _to_json_safe(tools_called) if tools_called is not None else None
        row = AiQueryAudit(
            user_id=user_id,
            conversation_id=conversation_id,
            prompt=(prompt or "")[:65535],
            refs=safe_refs,
            tools_called=safe_tools,
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
