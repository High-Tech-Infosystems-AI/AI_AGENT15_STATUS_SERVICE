"""Anomaly subscription CRUD — same pattern as scheduled queries."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai_chat_layer.models import AiAnomalySubscription, AiApproval
from app.ai_chat_layer.schemas import (
    AnomalySubCreate, AnomalySubOut, AnomalySubUpdate,
)
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import is_admin
from app.database_Layer.db_config import get_db

logger = logging.getLogger("app_logger")

router = APIRouter(prefix="/anomaly")


def _is_super(user: dict) -> bool:
    return (user.get("role_name") or "").lower() in {"super_admin", "superadmin", "super admin"}


def _approver_role_for(user: dict) -> str:
    if _is_super(user):
        return "self"
    if is_admin(user.get("role_name")):
        return "super"
    return "admin_or_super"


def _has_pending(db: Session, sub_id: int) -> bool:
    return bool(db.query(AiApproval).filter(
        AiApproval.target_kind == "anomaly",
        AiApproval.target_id == sub_id,
        AiApproval.status == "pending",
    ).first())


def _row_to_out(row: AiAnomalySubscription, *, pending: bool = False) -> AnomalySubOut:
    return AnomalySubOut(
        id=row.id, user_id=row.user_id, name=row.name, metric_key=row.metric_key,
        params=row.params, is_active=bool(row.is_active),
        cooldown_min=row.cooldown_min, last_fired_at=row.last_fired_at,
        created_at=row.created_at, pending_approval=pending,
    )


@router.post("", response_model=AnomalySubOut)
def create_sub(body: AnomalySubCreate,
               user: dict = Depends(current_user),
               db: Session = Depends(get_db)) -> AnomalySubOut:
    approver_role = _approver_role_for(user)
    auto_active = approver_role == "self"
    sub = AiAnomalySubscription(
        user_id=int(user["user_id"]),
        name=body.name, metric_key=body.metric_key,
        params=body.params,
        is_active=1 if auto_active else 0,
        cooldown_min=body.cooldown_min,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    if not auto_active:
        approval = AiApproval(
            user_id=int(user["user_id"]),
            origin="anomaly_create",
            payload={"anomaly_id": sub.id, "name": sub.name,
                     "metric_key": sub.metric_key},
            approver_role=approver_role,
            target_kind="anomaly", target_id=sub.id,
        )
        db.add(approval)
        db.commit()
    return _row_to_out(sub, pending=not auto_active)


@router.get("", response_model=List[AnomalySubOut])
def list_subs(user: dict = Depends(current_user),
              db: Session = Depends(get_db)) -> List[AnomalySubOut]:
    rows = (db.query(AiAnomalySubscription)
            .filter(AiAnomalySubscription.user_id == int(user["user_id"]))
            .order_by(AiAnomalySubscription.created_at.desc())
            .all())
    return [_row_to_out(r, pending=_has_pending(db, r.id)) for r in rows]


@router.patch("/{sub_id}", response_model=AnomalySubOut)
def update_sub(sub_id: int, body: AnomalySubUpdate,
               user: dict = Depends(current_user),
               db: Session = Depends(get_db)) -> AnomalySubOut:
    sub = db.get(AiAnomalySubscription, sub_id)
    if not sub or sub.user_id != int(user["user_id"]):
        raise HTTPException(status_code=404, detail="Subscription not found")
    if body.name is not None:
        sub.name = body.name
    if body.params is not None:
        sub.params = body.params
    if body.cooldown_min is not None:
        sub.cooldown_min = body.cooldown_min
    if body.is_active is not None:
        if body.is_active and _has_pending(db, sub_id):
            raise HTTPException(status_code=400, detail="Subscription is awaiting approval")
        sub.is_active = 1 if body.is_active else 0
    db.commit()
    db.refresh(sub)
    return _row_to_out(sub, pending=_has_pending(db, sub_id))


@router.delete("/{sub_id}")
def delete_sub(sub_id: int,
               user: dict = Depends(current_user),
               db: Session = Depends(get_db)) -> dict:
    sub = db.get(AiAnomalySubscription, sub_id)
    if not sub or sub.user_id != int(user["user_id"]):
        raise HTTPException(status_code=404, detail="Subscription not found")
    db.delete(sub)
    db.query(AiApproval).filter(
        AiApproval.target_kind == "anomaly",
        AiApproval.target_id == sub_id,
        AiApproval.status == "pending",
    ).update({"status": "expired"})
    db.commit()
    return {"deleted": True}
