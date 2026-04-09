"""
Notification Actions API — mark read / mark all read.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.dependencies.auth_utils import validate_token
from app.database_Layer.db_config import get_db
from app.notification_layer import store, redis_manager
from app.notification_layer.schemas import MarkReadResponse

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.put("/{notification_id}/read", response_model=MarkReadResponse)
async def mark_read(
    notification_id: int,
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Mark a single notification as read for the current user."""
    user_id = user_info.get("user_id")
    success = store.mark_notification_read(db, notification_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found for this user")

    redis_manager.invalidate_unread_count([user_id])
    return MarkReadResponse(success=True, message="Notification marked as read")


@router.put("/mark-all-read", response_model=MarkReadResponse)
async def mark_all_read(
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Mark all unread notifications as read for the current user."""
    user_id = user_info.get("user_id")
    count = store.mark_all_read(db, user_id)
    redis_manager.invalidate_unread_count([user_id])
    return MarkReadResponse(success=True, message=f"Marked {count} notifications as read")
