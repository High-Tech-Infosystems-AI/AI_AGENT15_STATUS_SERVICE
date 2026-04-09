"""
Get User Notifications API — paginated, filterable.
GET /notifications
GET /notifications/unread-count
"""

import math
import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.endpoints.dependencies.auth_utils import validate_token
from app.database_Layer.db_config import get_db
from app.notification_layer import store, redis_manager
from app.notification_layer.schemas import (
    NotificationListResponse, NotificationOut, PaginationDetails, UnreadCountResponse,
)

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.get("/", response_model=NotificationListResponse)
async def get_notifications(
    domain_type: Optional[str] = Query(None, description="Comma-separated: login,jobs,ai,candidate,security,system,user_management"),
    visibility: Optional[str] = Query(None, description="Comma-separated: personal,public,restricted"),
    date_from: Optional[str] = Query(None, description="ISO date: 2026-04-01"),
    date_to: Optional[str] = Query(None, description="ISO date: 2026-04-09"),
    priority: Optional[str] = Query(None, description="Comma-separated: low,medium,high,critical"),
    is_read: Optional[bool] = Query(None),
    delivery_mode: Optional[str] = Query(None, description="push or banner"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Get the authenticated user's notifications with full filtering."""
    user_id = user_info.get("user_id")

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

    results, total, unread = store.get_user_notifications(
        db=db,
        user_id=user_id,
        page=page,
        limit=limit,
        domain_type=domain_type,
        visibility=visibility,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        priority=priority,
        is_read=is_read,
        delivery_mode=delivery_mode,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    total_pages = math.ceil(total / limit) if total > 0 else 1

    return NotificationListResponse(
        notifications=[NotificationOut(**r) for r in results],
        pagination=PaginationDetails(
            page=page,
            limit=limit,
            total_pages=total_pages,
            total_elements=total,
        ),
        unread_count=unread,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Get unread notification count (cached in Redis for performance)."""
    user_id = user_info.get("user_id")

    # Try cache first
    cached = redis_manager.get_cached_unread_count(user_id)
    if cached is not None:
        return UnreadCountResponse(count=cached)

    count = store.get_unread_count(db, user_id)
    redis_manager.set_cached_unread_count(user_id, count)
    return UnreadCountResponse(count=count)
