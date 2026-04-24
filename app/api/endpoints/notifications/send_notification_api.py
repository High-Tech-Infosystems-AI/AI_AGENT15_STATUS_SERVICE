"""
Manual Send Notification API — admin/super_admin only.
POST /notifications/send
"""

import json
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.dependencies.auth_utils import validate_token, check_admin_access
from app.database_Layer.db_config import get_db
from app.notification_layer import store, redis_manager
from app.notification_layer.store import TargetValidationError
from app.notification_layer.schemas import SendNotificationRequest, SendNotificationResponse

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.post("/send", response_model=SendNotificationResponse)
async def send_notification(
    request: SendNotificationRequest,
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """Manually send a push notification. Admin/super_admin only."""
    role_name = user_info.get("role_name", "")
    if not check_admin_access(role_name):
        raise HTTPException(status_code=403, detail="Only admin/super_admin can send notifications")

    user_id = user_info.get("user_id")

    try:
        notif, recipient_ids = store.create_notification(
            db=db,
            title=request.title,
            message=request.message,
            delivery_mode=request.delivery_mode,
            domain_type=request.domain_type,
            visibility=request.visibility,
            priority=request.priority,
            target_type=request.target_type,
            target_id=request.target_id,
            source_service="system",
            event_type=None,
            metadata=request.metadata,
            created_by=user_id,
        )
    except TargetValidationError as e:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail={"code": e.code, "message": e.message},
        )

    # Publish to Redis
    pub_payload = {
        "id": notif.id,
        "title": notif.title,
        "message": notif.message,
        "delivery_mode": notif.delivery_mode,
        "domain_type": notif.domain_type,
        "visibility": notif.visibility,
        "priority": notif.priority,
        "source_service": "system",
        "metadata": request.metadata,
        "created_at": str(notif.created_at),
    }

    # Invalidate unread count caches first so the fresh counts are accurate
    redis_manager.invalidate_unread_count(recipient_ids)

    # Per-user per-mode unread counts — WS receives the full {push, banner, log, total} breakdown
    unread_counts_by_mode = store.get_unread_counts_by_mode_bulk(db, recipient_ids)

    if notif.delivery_mode == "banner":
        # Banners have a dedicated channel + per-user snapshot update so the
        # client's banner ticker re-renders.
        banner_payload = {
            "id": notif.id,
            "title": notif.title,
            "message": notif.message,
            "priority": notif.priority,
            "domain_type": notif.domain_type,
            "visibility": notif.visibility,
            "target_type": notif.target_type,
            "expires_at": str(notif.expires_at) if notif.expires_at else None,
            "created_at": str(notif.created_at),
            "recipient_ids": list(recipient_ids),
        }
        redis_manager.publish_banner("create", banner_payload)
        redis_manager.invalidate_banner_cache()

        # Per-user banner snapshot — so clients' "banners list" refreshes
        snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
        redis_manager.publish_banner_snapshots(snapshots)

        # Unread count event still fires (banner contributes to per-mode counts)
        for uid, counts in unread_counts_by_mode.items():
            redis_manager.publish_unread_count(uid, counts.get("push", 0), by_mode=counts)
    else:
        # push/log go through the regular per-user / broadcast channel
        if notif.visibility == "public" or request.target_type == "all":
            redis_manager.publish_broadcast(pub_payload, user_unread_counts=unread_counts_by_mode)
        else:
            redis_manager.publish_to_users(recipient_ids, pub_payload, unread_counts=unread_counts_by_mode)

    return SendNotificationResponse(
        success=True,
        notification_id=notif.id,
        recipients_count=len(recipient_ids),
        message=f"Notification sent to {len(recipient_ids)} recipients",
    )
