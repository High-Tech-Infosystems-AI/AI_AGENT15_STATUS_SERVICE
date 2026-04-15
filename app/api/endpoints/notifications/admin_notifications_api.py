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
from app.notification_layer import store, redis_manager
from app.notification_layer.schemas import (
    AdminNotificationListResponse, AdminNotificationOut, PaginationDetails,
    AdminStatsResponse, UpdateBannerExpiryRequest, BannerActionResponse,
    DeleteNotificationResponse,
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


def _require_admin(user_info: dict):
    role_name = user_info.get("role_name", "")
    if not check_admin_access(role_name):
        raise HTTPException(status_code=403, detail="Only admin/super_admin can perform this action")


def _parse_iso_date(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Admin Stats Summary
# ---------------------------------------------------------------------------

@router.get("/admin/stats", response_model=AdminStatsResponse)
async def get_admin_stats(
    date_from: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD). Inclusive."),
    date_to: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD). Inclusive."),
    user_info: dict = Depends(validate_token),
    db=Depends(get_db),
):
    """
    Summary stats for the admin dashboard:
    - total_notifications_sent — active notifications in the date range
    - notifications_scheduled — pending scheduled notifications
    - engagement_rate — % of delivered recipients that read the notification
    - delivery_success — % of notifications that reached at least one recipient
    """
    _require_admin(user_info)

    parsed_from = _parse_iso_date(date_from)
    parsed_to = _parse_iso_date(date_to)

    stats = store.get_admin_stats(db=db, date_from=parsed_from, date_to=parsed_to)
    return AdminStatsResponse(
        total_notifications_sent=stats["total_notifications_sent"],
        notifications_scheduled=stats["notifications_scheduled"],
        engagement_rate=stats["engagement_rate"],
        delivery_success=stats["delivery_success"],
        total_recipients=stats["total_recipients"],
        total_read=stats["total_read"],
        date_from=parsed_from,
        date_to=parsed_to,
    )


# ---------------------------------------------------------------------------
# Banner Management (admin/super_admin)
# ---------------------------------------------------------------------------

@router.put("/admin/banners/{banner_id}/expiry", response_model=BannerActionResponse)
async def change_banner_expiry(
    banner_id: int,
    request: UpdateBannerExpiryRequest,
    user_info: dict = Depends(validate_token),
    db=Depends(get_db),
):
    """Update the expiry date of an existing banner notification."""
    _require_admin(user_info)

    banner = store.update_banner_expiry(db, banner_id, request.expires_at)
    if not banner:
        raise HTTPException(status_code=404, detail="Banner not found")

    redis_manager.invalidate_banner_cache()

    # If this change expired the banner immediately, notify recipients
    if banner.is_active == 0:
        recipient_ids = store.get_banner_recipient_ids(db, banner_id)
        redis_manager.publish_banner("expire", {
            "id": banner_id,
            "recipient_ids": list(recipient_ids),
        })
        if recipient_ids:
            snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
            redis_manager.publish_banner_snapshots(snapshots)

    return BannerActionResponse(
        success=True,
        banner_id=banner_id,
        is_active=bool(banner.is_active),
        expires_at=banner.expires_at,
        message="Banner expiry updated",
    )


@router.put("/admin/banners/{banner_id}/expire-now", response_model=BannerActionResponse)
async def expire_banner_now(
    banner_id: int,
    user_info: dict = Depends(validate_token),
    db=Depends(get_db),
):
    """Expire a banner immediately (one-click expire). Notifies all connected recipients."""
    _require_admin(user_info)

    result = store.expire_banner_now(db, banner_id)
    if not result:
        raise HTTPException(status_code=404, detail="Banner not found")

    banner, recipient_ids = result

    # Publish expire event + updated snapshot to each affected user
    redis_manager.publish_banner("expire", {
        "id": banner_id,
        "recipient_ids": list(recipient_ids),
    })
    redis_manager.invalidate_banner_cache()
    if recipient_ids:
        snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
        redis_manager.publish_banner_snapshots(snapshots)

    return BannerActionResponse(
        success=True,
        banner_id=banner_id,
        is_active=False,
        expires_at=banner.expires_at,
        message=f"Banner expired. {len(recipient_ids)} users notified.",
    )


# ---------------------------------------------------------------------------
# Delete Notification (soft delete)
# ---------------------------------------------------------------------------

@router.delete("/admin/{notification_id}", response_model=DeleteNotificationResponse)
async def delete_notification(
    notification_id: int,
    user_info: dict = Depends(validate_token),
    db=Depends(get_db),
):
    """Soft-delete a notification (sets is_active=0). Admin/super_admin only.
    If it's a banner, connected recipients get an expire event immediately.
    Unread caches for all recipients are invalidated.
    """
    _require_admin(user_info)

    # Get recipient list BEFORE delete so we can notify them
    recipient_ids = store.get_notification_recipient_ids(db, notification_id)

    notif = store.soft_delete_notification(db, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")

    # If banner, notify connected clients to remove it from the ticker
    if notif.delivery_mode == "banner":
        redis_manager.publish_banner("expire", {
            "id": notification_id,
            "recipient_ids": list(recipient_ids),
        })
        redis_manager.invalidate_banner_cache()
        if recipient_ids:
            snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
            redis_manager.publish_banner_snapshots(snapshots)

    # Invalidate unread counts + push fresh counts to WS so deleted notif drops off
    if recipient_ids:
        redis_manager.invalidate_unread_count(recipient_ids)
        counts_by_mode = store.get_unread_counts_by_mode_bulk(db, recipient_ids)
        for uid, counts in counts_by_mode.items():
            redis_manager.publish_unread_count(uid, counts.get("push", 0), by_mode=counts)

    return DeleteNotificationResponse(
        success=True,
        notification_id=notification_id,
        message=f"Notification {notification_id} deleted ({notif.delivery_mode})",
    )
