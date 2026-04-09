"""
Banner Notification API.
POST /notifications/banner  — create banner (admin only)
GET  /notifications/banners/active — list active banners
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.api.endpoints.dependencies.auth_utils import validate_token, check_admin_access
from app.database_Layer.db_config import get_db
from app.notification_layer import store, redis_manager
from app.notification_layer.schemas import CreateBannerRequest, BannerResponse, SendNotificationResponse

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.post("/banner", response_model=SendNotificationResponse)
async def create_banner(
    request: CreateBannerRequest,
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Create a banner notification (scrolling dashboard ticker). Admin/super_admin only."""
    role_name = user_info.get("role_name", "")
    if not check_admin_access(role_name):
        raise HTTPException(status_code=403, detail="Only admin/super_admin can create banners")

    user_id = user_info.get("user_id")

    notif, recipient_ids = store.create_notification(
        db=db,
        title=request.title,
        message=request.message,
        delivery_mode="banner",
        domain_type=request.domain_type,
        visibility="public",
        priority=request.priority,
        target_type="all",
        target_id=None,
        source_service="system",
        metadata=request.metadata,
        created_by=user_id,
        expires_at=request.expires_at,
    )

    # Publish banner event to Redis
    pub_payload = {
        "id": notif.id,
        "title": notif.title,
        "message": notif.message,
        "priority": notif.priority,
        "domain_type": notif.domain_type,
        "expires_at": str(notif.expires_at) if notif.expires_at else None,
        "created_at": str(notif.created_at),
    }
    redis_manager.publish_banner("create", pub_payload)
    redis_manager.invalidate_banner_cache()
    redis_manager.invalidate_unread_count(recipient_ids)

    return SendNotificationResponse(
        success=True,
        notification_id=notif.id,
        recipients_count=len(recipient_ids),
        message=f"Banner created and sent to {len(recipient_ids)} users",
    )


@router.get("/banners/active", response_model=List[BannerResponse])
async def get_active_banners(
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Get all active banner notifications for the dashboard scrolling ticker."""
    # Try cache first
    cached = redis_manager.get_cached_banners()
    if cached is not None:
        return cached

    banners = store.get_active_banners(db)
    redis_manager.set_cached_banners(banners)
    return banners
