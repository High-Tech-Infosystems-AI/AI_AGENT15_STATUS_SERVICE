"""
Notification Scheduling API — admin/super_admin only.
POST /notifications/schedule
GET  /notifications/schedules
PUT  /notifications/schedules/{id}
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
from app.notification_layer.store import TargetValidationError
from app.notification_layer.schemas import (
    CreateScheduleRequest, ScheduleOut, ScheduleListResponse,
    PaginationDetails, MarkReadResponse, UpdateScheduleRequest,
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

    # Validate target upfront so admins get immediate feedback on bad job_id/user_id/role
    try:
        store.resolve_target_user_ids(
            db, request.target_type, request.target_id,
            include_admins=(request.target_type != "all"),
        )
    except TargetValidationError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})

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
        extra_metadata=json.dumps(request.metadata) if request.metadata else None,
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


@router.put("/schedules/{schedule_id}", response_model=ScheduleOut)
async def edit_schedule(
    schedule_id: int,
    request: UpdateScheduleRequest,
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Edit a pending scheduled notification. Admin/super_admin only."""
    role_name = user_info.get("role_name", "")
    if not check_admin_access(role_name):
        raise HTTPException(status_code=403, detail="Only admin/super_admin can edit schedules")

    provided_fields = set(request.model_fields_set)
    if not provided_fields:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    existing = store.get_schedule_by_id(db, schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if existing.status != "pending":
        raise HTTPException(status_code=404, detail="Schedule not found or already sent/cancelled")

    updated_target_type = request.target_type if "target_type" in provided_fields else existing.target_type
    updated_target_id = request.target_id if "target_id" in provided_fields else existing.target_id

    # If changing target to all, target_id is not needed and is cleared.
    if updated_target_type == "all":
        updated_target_id = None

    try:
        store.resolve_target_user_ids(
            db,
            updated_target_type,
            updated_target_id,
            include_admins=(updated_target_type != "all"),
        )
    except TargetValidationError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})

    updates = {}
    if "title" in provided_fields:
        updates["title"] = request.title
    if "message" in provided_fields:
        updates["message"] = request.message
    if "delivery_mode" in provided_fields:
        updates["delivery_mode"] = request.delivery_mode
    if "domain_type" in provided_fields:
        updates["domain_type"] = request.domain_type
    if "visibility" in provided_fields:
        updates["visibility"] = request.visibility
    if "priority" in provided_fields:
        updates["priority"] = request.priority
    if "scheduled_at" in provided_fields:
        updates["scheduled_at"] = request.scheduled_at
    if "repeat_type" in provided_fields:
        updates["repeat_type"] = request.repeat_type
    if "repeat_until" in provided_fields:
        updates["repeat_until"] = request.repeat_until
    if "metadata" in provided_fields:
        updates["extra_metadata"] = json.dumps(request.metadata) if request.metadata else None

    updates["target_type"] = updated_target_type
    updates["target_id"] = updated_target_id

    sched = store.update_schedule(db, schedule_id, updates)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found or already sent/cancelled")

    return ScheduleOut.model_validate(sched)


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
