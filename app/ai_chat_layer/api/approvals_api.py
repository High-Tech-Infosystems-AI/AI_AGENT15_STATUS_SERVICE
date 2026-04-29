"""Approval queue endpoints.

  GET    /ai-chat/approvals/pending — items the caller's role can act on
  POST   /ai-chat/approvals/{id}    — { decision: 'approve' | 'decline' }

When approved, the linked schedule/anomaly_subscription is activated and
its `next_run_at` is materialized.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai_chat_layer.api.schedules_api import _next_run_at
from app.ai_chat_layer.models import (
    AiAnomalySubscription, AiApproval, AiScheduledQuery,
)
from app.ai_chat_layer.schemas import (
    ApprovalDecisionRequest, ApprovalOut, ApprovalsPage,
)
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import is_admin
from app.database_Layer.db_config import get_db

router = APIRouter(prefix="/approvals")


def _is_super(user: dict) -> bool:
    return (user.get("role_name") or "").lower() in {"super_admin", "superadmin", "super admin"}


def _row_to_out(row: AiApproval) -> ApprovalOut:
    return ApprovalOut(
        id=row.id, user_id=row.user_id, origin=row.origin, payload=row.payload,
        status=row.status, approver_role=row.approver_role,
        target_kind=row.target_kind, target_id=row.target_id,
        decided_by=row.decided_by, decided_at=row.decided_at,
        created_at=row.created_at,
    )


def _allowed_roles_for(user: dict) -> List[str]:
    """What approver_role values can this caller act on?"""
    if _is_super(user):
        return ["super", "admin_or_super"]
    if is_admin(user.get("role_name")):
        return ["admin_or_super"]
    return []


@router.get("/pending", response_model=ApprovalsPage)
def pending(user: dict = Depends(current_user),
            db: Session = Depends(get_db)) -> ApprovalsPage:
    roles = _allowed_roles_for(user)
    if not roles:
        return ApprovalsPage(items=[], total=0)
    rows = (db.query(AiApproval)
            .filter(AiApproval.status == "pending")
            .filter(AiApproval.approver_role.in_(roles))
            .order_by(AiApproval.created_at.desc())
            .limit(200)
            .all())
    return ApprovalsPage(items=[_row_to_out(r) for r in rows], total=len(rows))


@router.post("/{approval_id}", response_model=ApprovalOut)
def decide(approval_id: int, body: ApprovalDecisionRequest,
           user: dict = Depends(current_user),
           db: Session = Depends(get_db)) -> ApprovalOut:
    row = db.get(AiApproval, approval_id)
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    if row.status != "pending":
        raise HTTPException(status_code=400,
                            detail=f"Approval already {row.status}")
    allowed = _allowed_roles_for(user)
    if row.approver_role not in allowed:
        raise HTTPException(status_code=403, detail="Not authorized to decide")

    decision = "approved" if body.decision == "approve" else "declined"
    row.status = decision
    row.decided_by = int(user["user_id"])
    row.decided_at = datetime.utcnow()

    # On approval, activate the linked target.
    if decision == "approved":
        if row.target_kind == "schedule" and row.target_id:
            sched = db.get(AiScheduledQuery, row.target_id)
            if sched:
                sched.is_active = 1
                sched.next_run_at = _next_run_at(sched.cron_expr)
        elif row.target_kind == "anomaly" and row.target_id:
            sub = db.get(AiAnomalySubscription, row.target_id)
            if sub:
                sub.is_active = 1
    db.commit()
    db.refresh(row)
    return _row_to_out(row)
