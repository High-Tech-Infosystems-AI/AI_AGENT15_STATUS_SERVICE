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
from app.notification_layer.store import TargetValidationError
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

    try:
        notif, recipient_ids = store.create_notification(
            db=db,
            title=request.title,
            message=request.message,
            delivery_mode="banner",
            domain_type=request.domain_type,
            visibility=request.visibility,
            priority=request.priority,
            target_type=request.target_type,
            target_id=request.target_id,
            source_service="system",
            metadata=request.metadata,
            created_by=user_id,
            expires_at=request.expires_at,
        )
    except TargetValidationError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})

    # Publish banner event to Redis with recipient list
    # so the WS fan-out only delivers to actual recipients.
    pub_payload = {
        "id": notif.id,
        "title": notif.title,
        "message": notif.message,
        "priority": notif.priority,
        "domain_type": notif.domain_type,
        "visibility": notif.visibility,
        "target_type": notif.target_type,
        "expires_at": str(notif.expires_at) if notif.expires_at else None,
        "created_at": str(notif.created_at),
        # Special field used by WS managers to route the banner
        "recipient_ids": list(recipient_ids),
    }
    redis_manager.publish_banner("create", pub_payload)
    redis_manager.invalidate_banner_cache()

    # Publish updated full banner snapshot to each affected user
    # so clients always have the complete current state, not deltas.
    snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
    redis_manager.publish_banner_snapshots(snapshots)

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
