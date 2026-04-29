"""Scheduled query CRUD + approval flow.

A schedule starts dormant (`is_active=0`) and gets paired with an
`ai_approval` row. Approval rules:

  - Recruiter (non-admin) creates → approver_role = 'admin_or_super'
  - Admin creates                  → approver_role = 'super'
  - SuperAdmin creates             → approver_role = 'self', auto-approved

When approval lands the schedule's `is_active` flips to 1 and `next_run_at`
materializes from the cron. The user can pause/resume from the UI; that
also updates `next_run_at` lazily.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai_chat_layer.models import (
    AiAnomalySubscription, AiApproval, AiScheduledQuery,
)
from app.ai_chat_layer.schemas import (
    ScheduledQueryCreate, ScheduledQueryOut, ScheduledQueryUpdate,
)
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import is_admin
from app.database_Layer.db_config import get_db

logger = logging.getLogger("app_logger")

router = APIRouter(prefix="/schedules")


def _is_super(user: dict) -> bool:
    return (user.get("role_name") or "").lower() in {"super_admin", "superadmin", "super admin"}


def _approver_role_for(user: dict) -> str:
    if _is_super(user):
        return "self"
    if is_admin(user.get("role_name")):
        return "super"
    return "admin_or_super"


def _next_run_at(cron_expr: str, base: Optional[datetime] = None) -> Optional[datetime]:
    """Compute the next occurrence. Falls back to None if croniter is missing."""
    try:
        from croniter import croniter  # type: ignore
    except Exception:
        return None
    base = base or datetime.utcnow()
    try:
        return croniter(cron_expr, base).get_next(datetime)
    except Exception:
        return None


def _row_to_out(row: AiScheduledQuery, *, pending: bool = False) -> ScheduledQueryOut:
    return ScheduledQueryOut(
        id=row.id, user_id=row.user_id, name=row.name, prompt=row.prompt,
        refs=row.refs, cron_expr=row.cron_expr, timezone=row.timezone,
        is_active=bool(row.is_active), last_run_at=row.last_run_at,
        next_run_at=row.next_run_at, created_at=row.created_at,
        pending_approval=pending,
    )


def _has_pending_approval(db: Session, schedule_id: int) -> bool:
    return bool(db.query(AiApproval).filter(
        AiApproval.target_kind == "schedule",
        AiApproval.target_id == schedule_id,
        AiApproval.status == "pending",
    ).first())


@router.post("", response_model=ScheduledQueryOut)
def create_schedule(body: ScheduledQueryCreate,
                    user: dict = Depends(current_user),
                    db: Session = Depends(get_db)) -> ScheduledQueryOut:
    approver_role = _approver_role_for(user)
    auto_active = approver_role == "self"
    sched = AiScheduledQuery(
        user_id=int(user["user_id"]),
        name=body.name, prompt=body.prompt,
        refs=[r.model_dump() for r in body.refs] if body.refs else None,
        cron_expr=body.cron_expr, timezone=body.timezone,
        is_active=1 if auto_active else 0,
        next_run_at=_next_run_at(body.cron_expr) if auto_active else None,
    )
    db.add(sched)
    db.commit()
    db.refresh(sched)
    if not auto_active:
        approval = AiApproval(
            user_id=int(user["user_id"]),
            origin="schedule_create",
            payload={"schedule_id": sched.id, "name": sched.name,
                     "cron_expr": sched.cron_expr},
            approver_role=approver_role,
            target_kind="schedule",
            target_id=sched.id,
        )
        db.add(approval)
        db.commit()
    return _row_to_out(sched, pending=not auto_active)


@router.get("", response_model=List[ScheduledQueryOut])
def list_schedules(user: dict = Depends(current_user),
                   db: Session = Depends(get_db)) -> List[ScheduledQueryOut]:
    rows = (db.query(AiScheduledQuery)
            .filter(AiScheduledQuery.user_id == int(user["user_id"]))
            .order_by(AiScheduledQuery.created_at.desc())
            .all())
    out = []
    for r in rows:
        out.append(_row_to_out(r, pending=_has_pending_approval(db, r.id)))
    return out


@router.patch("/{schedule_id}", response_model=ScheduledQueryOut)
def update_schedule(schedule_id: int, body: ScheduledQueryUpdate,
                    user: dict = Depends(current_user),
                    db: Session = Depends(get_db)) -> ScheduledQueryOut:
    sched = db.get(AiScheduledQuery, schedule_id)
    if not sched or sched.user_id != int(user["user_id"]):
        raise HTTPException(status_code=404, detail="Schedule not found")
    if body.name is not None:
        sched.name = body.name
    if body.prompt is not None:
        sched.prompt = body.prompt
    if body.cron_expr is not None:
        sched.cron_expr = body.cron_expr
    if body.timezone is not None:
        sched.timezone = body.timezone
    if body.is_active is not None:
        # Cannot self-activate while approval is still pending.
        if body.is_active and _has_pending_approval(db, schedule_id):
            raise HTTPException(status_code=400,
                                detail="Schedule is awaiting approval")
        sched.is_active = 1 if body.is_active else 0
        if body.is_active:
            sched.next_run_at = _next_run_at(sched.cron_expr)
    db.commit()
    db.refresh(sched)
    return _row_to_out(sched, pending=_has_pending_approval(db, schedule_id))


@router.delete("/{schedule_id}")
def delete_schedule(schedule_id: int,
                    user: dict = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    sched = db.get(AiScheduledQuery, schedule_id)
    if not sched or sched.user_id != int(user["user_id"]):
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.delete(sched)
    # Mark any pending approval as expired so the queue doesn't dangle.
    db.query(AiApproval).filter(
        AiApproval.target_kind == "schedule",
        AiApproval.target_id == schedule_id,
        AiApproval.status == "pending",
    ).update({"status": "expired"})
    db.commit()
    return {"deleted": True}
