"""
Admin Notification Log API — super_admin/admin see ALL notifications.
GET /notifications/admin/logs
"""

import math
import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.dependencies.auth_utils import validate_token, check_admin_access
from app.database_Layer.db_config import get_db
from app.notification_layer import store
from app.notification_layer.schemas import (
    AdminNotificationListResponse, AdminNotificationOut, PaginationDetails,
)

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.get("/admin/logs", response_model=AdminNotificationListResponse)
async def get_admin_notification_logs(
    domain_type: Optional[str] = Query(None, description="Comma-separated: login,jobs,ai,candidate,security,system,user_management,manual"),
    visibility: Optional[str] = Query(None, description="Comma-separated: personal,public,restricted"),
    date_from: Optional[str] = Query(None, description="ISO date: 2026-04-01"),
    date_to: Optional[str] = Query(None, description="ISO date: 2026-04-09"),
    priority: Optional[str] = Query(None, description="Comma-separated: low,medium,high,critical"),
    source_service: Optional[str] = Query(None, description="Comma-separated: login,job,candidate,resume_analyzer,rbac,bulk_candidate,system"),
    event_type: Optional[str] = Query(None, description="Comma-separated event names"),
    user_id: Optional[str] = Query(None, description="Comma-separated user IDs — notifications sent to these users"),
    job_id: Optional[str] = Query(None, description="Comma-separated job IDs — matches target_id (when target_type=job) OR metadata.job_id"),
    company_id: Optional[str] = Query(None, description="Comma-separated company IDs — matches metadata.company_id"),
    created_by: Optional[str] = Query(None, description="Comma-separated admin user IDs who created the notifications"),
    delivery_mode: Optional[str] = Query(None, description="Comma-separated: push,banner,log"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Admin audit log — view ALL notifications in the system with full filtering.
    Every ID filter (user_id, job_id, company_id, created_by) accepts comma-separated values.
    """
    role_name = user_info.get("role_name", "")
    if not check_admin_access(role_name):
        raise HTTPException(status_code=403, detail="Only admin/super_admin can access notification logs")

    parsed_date_from = None
    parsed_date_to = None
    if date_from:
        try:
            parsed_date_from = datetime.fromisoformat(date_from)
        except ValueError:
            pass
    if date_to:
        try:
            parsed_date_to = datetime.fromisoformat(date_to)
        except ValueError:
            pass

    results, total = store.get_admin_notification_logs(
        db=db,
        page=page,
        limit=limit,
        domain_type=domain_type,
        visibility=visibility,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        priority=priority,
        source_service=source_service,
        event_type=event_type,
        user_id=user_id,
        job_id=job_id,
        company_id=company_id,
        created_by=created_by,
        delivery_mode=delivery_mode,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    total_pages = math.ceil(total / limit) if total > 0 else 1

    return AdminNotificationListResponse(
        notifications=[AdminNotificationOut(**r) for r in results],
        pagination=PaginationDetails(
            page=page,
            limit=limit,
            total_pages=total_pages,
            total_elements=total,
        ),
    )
