"""
Notification Scheduling API — admin/super_admin only.
POST /notifications/schedule
GET  /notifications/schedules
PUT  /notifications/schedules/{id}/cancel
"""

import math
import json
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.endpoints.dependencies.auth_utils import validate_token, check_admin_access
from app.database_Layer.db_config import get_db
from app.notification_layer import store
from app.notification_layer.schemas import (
    CreateScheduleRequest, ScheduleOut, ScheduleListResponse,
    PaginationDetails, MarkReadResponse,
)

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.post("/schedule", response_model=ScheduleOut)
async def create_schedule(
    request: CreateScheduleRequest,
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Create a scheduled notification. Admin/super_admin only."""
    role_name = user_info.get("role_name", "")
    if not check_admin_access(role_name):
        raise HTTPException(status_code=403, detail="Only admin/super_admin can schedule notifications")

    user_id = user_info.get("user_id")

    sched = store.create_schedule(
        db=db,
        title=request.title,
        message=request.message,
        delivery_mode=request.delivery_mode,
        domain_type=request.domain_type,
        visibility=request.visibility,
        priority=request.priority,
        target_type=request.target_type,
        target_id=request.target_id,
        metadata=json.dumps(request.metadata) if request.metadata else None,
        scheduled_at=request.scheduled_at,
        repeat_type=request.repeat_type,
        repeat_until=request.repeat_until,
        created_by=user_id,
    )
    return ScheduleOut.model_validate(sched)


@router.get("/schedules", response_model=ScheduleListResponse)
async def list_schedules(
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """List all scheduled notifications. Admin/super_admin only."""
    role_name = user_info.get("role_name", "")
    if not check_admin_access(role_name):
        raise HTTPException(status_code=403, detail="Only admin/super_admin can view schedules")

    schedules, total = store.get_schedules(db=db, page=page, limit=limit)
    total_pages = math.ceil(total / limit) if total > 0 else 1

    return ScheduleListResponse(
        schedules=[ScheduleOut.model_validate(s) for s in schedules],
        pagination=PaginationDetails(
            page=page,
            limit=limit,
            total_pages=total_pages,
            total_elements=total,
        ),
    )


@router.put("/schedules/{schedule_id}/cancel", response_model=MarkReadResponse)
async def cancel_schedule(
    schedule_id: int,
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Cancel a pending scheduled notification. Admin/super_admin only."""
    role_name = user_info.get("role_name", "")
    if not check_admin_access(role_name):
        raise HTTPException(status_code=403, detail="Only admin/super_admin can cancel schedules")

    success = store.cancel_schedule(db, schedule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Schedule not found or already sent/cancelled")

    return MarkReadResponse(success=True, message="Schedule cancelled")
