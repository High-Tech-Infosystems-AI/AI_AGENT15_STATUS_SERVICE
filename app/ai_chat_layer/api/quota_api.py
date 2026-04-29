"""Token quota REST endpoints.

  GET    /ai-chat/quota/me           — caller usage + limits
  GET    /ai-chat/quota/users        — SuperAdmin only, paginated list
  PATCH  /ai-chat/quota/users/{uid}  — SuperAdmin only, edit limits

Limit edits are recorded in `ai_query_audit` via `updated_by` so changes
are traceable to the SuperAdmin who made them.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.ai_chat_layer import quota
from app.ai_chat_layer.models import AiTokenQuota
from app.ai_chat_layer.schemas import (
    QuotaOut, QuotaUpdate, UserQuotaList, UserQuotaRow,
)
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import is_admin
from app.database_Layer.db_config import get_db

logger = logging.getLogger("app_logger")

router = APIRouter(prefix="/quota")


def _is_super(user: dict) -> bool:
    name = (user.get("role_name") or "").lower()
    return name in {"super_admin", "superadmin", "super admin"}


@router.get("/me", response_model=QuotaOut)
def my_quota(user: dict = Depends(current_user),
             db: Session = Depends(get_db)) -> QuotaOut:
    return QuotaOut(**quota.status_for(db, int(user["user_id"])))


@router.get("/users", response_model=UserQuotaList)
def list_user_quotas(q: Optional[str] = None,
                     limit: int = Query(default=50, ge=1, le=500),
                     offset: int = Query(default=0, ge=0),
                     user: dict = Depends(current_user),
                     db: Session = Depends(get_db)) -> UserQuotaList:
    if not _is_super(user):
        raise HTTPException(status_code=403, detail="SuperAdmin only")

    where = "WHERE u.deleted_at IS NULL"
    params: dict = {"_limit": limit, "_offset": offset}
    if q:
        where += " AND (u.name LIKE :q OR u.username LIKE :q OR u.email LIKE :q)"
        params["q"] = f"%{q}%"

    rows = db.execute(text(f"""
        SELECT u.id, u.name, u.username,
               COALESCE(r.name, '') AS role_name,
               COALESCE(q.daily_limit, :default_daily) AS daily_limit,
               COALESCE(q.monthly_limit, :default_monthly) AS monthly_limit,
               COALESCE(q.used_today, 0) AS used_today,
               COALESCE(q.used_month, 0) AS used_month
          FROM users u
     LEFT JOIN roles r ON r.id = u.role_id
     LEFT JOIN ai_token_quota q ON q.user_id = u.id
        {where}
         ORDER BY u.id
         LIMIT :_limit OFFSET :_offset
    """), {**params,
            "default_daily": 50000,
            "default_monthly": 1000000}).all()

    total = db.execute(text(f"""
        SELECT COUNT(*) FROM users u
         {where}
    """), {k: v for k, v in params.items() if k not in ("_limit", "_offset")}).scalar() or 0

    items = [UserQuotaRow(
        user_id=r._mapping["id"], name=r._mapping["name"],
        username=r._mapping["username"], role_name=r._mapping["role_name"],
        daily_limit=int(r._mapping["daily_limit"] or 0),
        monthly_limit=int(r._mapping["monthly_limit"] or 0),
        used_today=int(r._mapping["used_today"] or 0),
        used_month=int(r._mapping["used_month"] or 0),
    ) for r in rows]
    return UserQuotaList(items=items, total=int(total))


@router.patch("/users/{target_user_id}", response_model=QuotaOut)
def update_user_quota(target_user_id: int, body: QuotaUpdate,
                      user: dict = Depends(current_user),
                      db: Session = Depends(get_db)) -> QuotaOut:
    if not _is_super(user):
        raise HTTPException(status_code=403, detail="SuperAdmin only")
    if body.daily_limit is None and body.monthly_limit is None:
        raise HTTPException(status_code=400, detail="Provide at least one limit")
    quota.set_limits(
        db,
        target_user_id=target_user_id,
        updated_by=int(user["user_id"]),
        daily_limit=body.daily_limit,
        monthly_limit=body.monthly_limit,
    )
    return QuotaOut(**quota.status_for(db, target_user_id))
