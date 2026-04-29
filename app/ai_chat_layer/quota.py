"""Per-user LLM token budget — daily + monthly, edited by SuperAdmin.

Pattern:
  1. `check(user_id, est)` before the call — raises QuotaExceededError if no
     room. Caller can pass a small estimate or zero to just probe.
  2. After the call, `commit(user_id, actual)` debits the actual token spend.

Day/month rollovers handled lazily on read: if the row's `day_anchor` is
older than today, `used_today` resets to zero before the limit check.
Same for `month_anchor`.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.ai_chat_layer.models import AiTokenQuota
from app.core import settings

logger = logging.getLogger("app_logger")


class QuotaExceededError(Exception):
    """Raised when the requested token spend would exceed daily or monthly."""

    def __init__(self, scope: str, used: int, limit: int):
        self.scope = scope          # "daily" | "monthly"
        self.used = used
        self.limit = limit
        super().__init__(f"AI {scope} token limit reached ({used}/{limit})")


def _month_anchor(d: date) -> str:
    return d.strftime("%Y-%m")


def _ensure_row(db: Session, user_id: int) -> AiTokenQuota:
    row = db.get(AiTokenQuota, user_id)
    today = date.today()
    if row is None:
        row = AiTokenQuota(
            user_id=user_id,
            daily_limit=int(getattr(settings, "AI_DEFAULT_DAILY_LIMIT", 50000)),
            monthly_limit=int(getattr(settings, "AI_DEFAULT_MONTHLY_LIMIT", 1000000)),
            used_today=0,
            used_month=0,
            day_anchor=today,
            month_anchor=_month_anchor(today),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    # Lazy rollovers
    dirty = False
    if row.day_anchor != today:
        row.used_today = 0
        row.day_anchor = today
        dirty = True
    if row.month_anchor != _month_anchor(today):
        row.used_month = 0
        row.month_anchor = _month_anchor(today)
        dirty = True
    if dirty:
        db.commit()
        db.refresh(row)
    return row


def check(db: Session, user_id: int, est_tokens: int = 0) -> AiTokenQuota:
    """Reads the row (creating + rolling over if needed) and rejects the
    call when adding `est_tokens` would breach a limit. Returns the row."""
    row = _ensure_row(db, user_id)
    if est_tokens and row.used_today + est_tokens > row.daily_limit:
        raise QuotaExceededError("daily", row.used_today, row.daily_limit)
    if est_tokens and row.used_month + est_tokens > row.monthly_limit:
        raise QuotaExceededError("monthly", row.used_month, row.monthly_limit)
    return row


def commit(db: Session, user_id: int, tokens: int) -> None:
    """Debit `tokens` against today + this month. Negative or zero is a no-op."""
    if tokens <= 0:
        return
    row = _ensure_row(db, user_id)
    row.used_today = (row.used_today or 0) + int(tokens)
    row.used_month = (row.used_month or 0) + int(tokens)
    db.commit()


def status_for(db: Session, user_id: int) -> dict:
    """Snapshot used by GET /ai-chat/quota/me."""
    row = _ensure_row(db, user_id)
    daily_pct = (row.used_today / row.daily_limit * 100) if row.daily_limit else 0.0
    month_pct = (row.used_month / row.monthly_limit * 100) if row.monthly_limit else 0.0
    return {
        "user_id": row.user_id,
        "daily_limit": row.daily_limit,
        "monthly_limit": row.monthly_limit,
        "used_today": row.used_today,
        "used_month": row.used_month,
        "day_anchor": row.day_anchor,
        "month_anchor": row.month_anchor,
        "percent_today": round(daily_pct, 2),
        "percent_month": round(month_pct, 2),
    }


def set_limits(db: Session, *, target_user_id: int, updated_by: int,
               daily_limit: Optional[int] = None,
               monthly_limit: Optional[int] = None) -> AiTokenQuota:
    row = _ensure_row(db, target_user_id)
    if daily_limit is not None:
        row.daily_limit = int(daily_limit)
    if monthly_limit is not None:
        row.monthly_limit = int(monthly_limit)
    row.updated_by = updated_by
    db.commit()
    db.refresh(row)
    return row
