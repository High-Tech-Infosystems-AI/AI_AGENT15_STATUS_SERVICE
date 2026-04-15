"""
Notification Actions API — mark read / mark unread / mark all read.

All actions publish the fresh unread count to the user's Redis channel so
every connected WebSocket tab for that user gets an instant badge update.
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


def _push_fresh_count(db: Session, user_id: int) -> int:
    """Recompute unread counts (push/banner/log), refresh cache, publish to user's WS channel.
    Returns the main (push-only) count.
    """
    redis_manager.invalidate_unread_count([user_id])
    by_mode = store.get_unread_counts_by_mode(db, user_id)
    main_count = by_mode["push"]
    redis_manager.set_cached_unread_count(user_id, main_count)
    redis_manager.publish_unread_count(user_id, main_count, by_mode=by_mode)
    return main_count


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

    count = _push_fresh_count(db, user_id)
    return MarkReadResponse(success=True, message=f"Notification marked as read (unread now: {count})")


@router.put("/{notification_id}/unread", response_model=MarkReadResponse)
async def mark_unread(
    notification_id: int,
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Mark a single notification as unread for the current user."""
    user_id = user_info.get("user_id")
    success = store.mark_notification_unread(db, notification_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found for this user")

    count = _push_fresh_count(db, user_id)
    return MarkReadResponse(success=True, message=f"Notification marked as unread (unread now: {count})")


@router.put("/mark-all-read", response_model=MarkReadResponse)
async def mark_all_read(
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Mark all unread notifications as read for the current user."""
    user_id = user_info.get("user_id")
    updated = store.mark_all_read(db, user_id)
    count = _push_fresh_count(db, user_id)
    return MarkReadResponse(success=True, message=f"Marked {updated} notifications as read (unread now: {count})")
