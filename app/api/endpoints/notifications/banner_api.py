"""
Banner Notification API.
POST /notifications/banner  — create banner (admin only)
GET  /notifications/banners/active — list active banners
"""

import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.api.endpoints.dependencies.auth_utils import validate_token, check_admin_access
from app.database_Layer.db_config import get_db
from app.notification_layer import store, redis_manager
from app.notification_layer.store import TargetValidationError
from app.notification_layer.schemas import CreateBannerRequest, BannerResponse, SendNotificationResponse

logger = logging.getLogger("app_logger")
router = APIRouter()


def _resolve_banner_expiry(requested: Optional[datetime]) -> datetime:
    """
    Validate & default the banner's expires_at.

    Rules:
    - If NULL/missing → next day 00:00 UTC (end-of-today convention).
    - If in the past → HTTP 400 (the banner would be dead on arrival).
    - Otherwise → honor the admin's choice.
    """
    now = datetime.utcnow()

    if requested is None:
        # Default = tomorrow 00:00 UTC (fresh start daily, matches auto-events)
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Strip timezone if present (the server stores naive UTC)
    if requested.tzinfo is not None:
        requested = requested.replace(tzinfo=None)

    if requested <= now:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "EXPIRES_IN_PAST",
                "message": f"expires_at ({requested.isoformat()}) must be in the future. "
                           f"Server time is {now.isoformat()}.",
            },
        )
    return requested


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

    # Validate + default expires_at (rejects past times, defaults to tomorrow 00:00)
    expires_at = _resolve_banner_expiry(request.expires_at)

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
            expires_at=expires_at,
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
