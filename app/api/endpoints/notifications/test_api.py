"""
Test-mode notification endpoints — NO JWT auth required.
Used by the Notification UI test dashboard.

All endpoints take user_id as a query parameter instead of extracting from JWT.
Prefix: /test
"""

import math
import json
import logging
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, Query, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database_Layer.db_config import get_db
from app.notification_layer import store, redis_manager
from app.notification_layer.store import TargetValidationError
from app.notification_layer.event_handler import handle_event
from app.notification_layer.schemas import (
    NotificationListResponse, NotificationOut, PaginationDetails,
    UnreadCountResponse, MarkReadResponse,
    AdminNotificationListResponse, AdminNotificationOut,
    SendNotificationRequest, SendNotificationResponse,
    CreateBannerRequest, BannerResponse,
    CreateScheduleRequest, ScheduleOut, ScheduleListResponse,
    EventTriggerRequest, EventTriggerResponse,
)

logger = logging.getLogger("app_logger")
router = APIRouter()


# --- Users (no auth) ---

@router.get("/users")
def get_all_users(db: Session = Depends(get_db)):
    """Get all users from the shared DB. No auth required."""
    rows = db.execute(text(
        "SELECT u.id, u.name, u.username, u.email, r.name as role_name "
        "FROM users u LEFT JOIN roles r ON u.role_id = r.id "
        "WHERE u.deleted_at IS NULL ORDER BY u.id"
    )).fetchall()
    return [
        {"id": r[0], "name": r[1], "username": r[2], "email": r[3], "role": r[4]}
        for r in rows
    ]


@router.get("/debug/recipients")
def debug_recipients(notification_id: Optional[int] = Query(None), user_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    """Debug: show notification recipients. Helps verify data is being created."""
    q = "SELECT nr.id, nr.notification_id, nr.user_id, nr.is_read, n.title, n.domain_type " \
        "FROM notification_recipients nr JOIN notifications n ON n.id = nr.notification_id WHERE 1=1"
    params = {}
    if notification_id:
        q += " AND nr.notification_id = :nid"
        params["nid"] = notification_id
    if user_id:
        q += " AND nr.user_id = :uid"
        params["uid"] = user_id
    q += " ORDER BY nr.id DESC LIMIT 50"
    rows = db.execute(text(q), params).fetchall()
    return [{"id": r[0], "notification_id": r[1], "user_id": r[2], "is_read": r[3], "title": r[4], "domain_type": r[5]} for r in rows]


@router.get("/debug/notifications")
def debug_notifications(db: Session = Depends(get_db)):
    """Debug: show latest notifications."""
    rows = db.execute(text(
        "SELECT id, title, domain_type, visibility, target_type, target_id, "
        "delivery_mode, is_active, created_at FROM notifications ORDER BY id DESC LIMIT 20"
    )).fetchall()
    return [{"id": r[0], "title": r[1], "domain_type": r[2], "visibility": r[3],
             "target_type": r[4], "target_id": r[5], "delivery_mode": r[6],
             "is_active": r[7], "created_at": str(r[8])} for r in rows]


# --- Notifications (user_id from query param) ---

@router.get("/notifications")
def get_notifications(
    user_id: int = Query(1),
    domain_type: Optional[str] = Query(None),
    visibility: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    is_read: Optional[bool] = Query(None),
    delivery_mode: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
):
    parsed_from = None
    parsed_to = None
    if date_from:
        try:
            parsed_from = datetime.fromisoformat(date_from)
        except ValueError:
            pass
    if date_to:
        try:
            parsed_to = datetime.fromisoformat(date_to)
        except ValueError:
            pass

    results, total, unread = store.get_user_notifications(
        db=db, user_id=user_id, page=page, limit=limit,
        domain_type=domain_type, visibility=visibility,
        date_from=parsed_from, date_to=parsed_to,
        priority=priority, is_read=is_read,
        delivery_mode=delivery_mode, sort_by=sort_by, sort_order=sort_order,
    )
    total_pages = math.ceil(total / limit) if total > 0 else 1
    return NotificationListResponse(
        notifications=[NotificationOut(**r) for r in results],
        pagination=PaginationDetails(page=page, limit=limit, total_pages=total_pages, total_elements=total),
        unread_count=unread,
    )


@router.get("/notifications/unread-count")
def get_unread_count(user_id: int = Query(1), db: Session = Depends(get_db)):
    by_mode = store.get_unread_counts_by_mode(db, user_id)
    redis_manager.set_cached_unread_count(user_id, by_mode["push"])
    return UnreadCountResponse(
        count=by_mode["push"],
        push=by_mode["push"],
        banner=by_mode["banner"],
        log=by_mode["log"],
        total=by_mode["total"],
    )


def _push_fresh_count(db: Session, user_id: int) -> int:
    """Recompute per-mode unread counts, refresh cache, publish to WS channel.
    Returns the main (push) count.
    """
    redis_manager.invalidate_unread_count([user_id])
    by_mode = store.get_unread_counts_by_mode(db, user_id)
    main_count = by_mode["push"]
    redis_manager.set_cached_unread_count(user_id, main_count)
    redis_manager.publish_unread_count(user_id, main_count, by_mode=by_mode)
    return main_count


@router.put("/notifications/{notification_id}/read")
def mark_read(notification_id: int, user_id: int = Query(1), db: Session = Depends(get_db)):
    success = store.mark_notification_read(db, notification_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found for this user")
    count = _push_fresh_count(db, user_id)
    return MarkReadResponse(success=True, message=f"Notification marked as read (unread now: {count})")


@router.put("/notifications/{notification_id}/unread")
def mark_unread(notification_id: int, user_id: int = Query(1), db: Session = Depends(get_db)):
    success = store.mark_notification_unread(db, notification_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found for this user")
    count = _push_fresh_count(db, user_id)
    return MarkReadResponse(success=True, message=f"Notification marked as unread (unread now: {count})")


@router.put("/notifications/mark-all-read")
def mark_all_read(user_id: int = Query(1), db: Session = Depends(get_db)):
    updated = store.mark_all_read(db, user_id)
    count = _push_fresh_count(db, user_id)
    return MarkReadResponse(success=True, message=f"Marked {updated} notifications as read (unread now: {count})")


# --- Banners ---

@router.get("/notifications/banners/active", response_model=List[BannerResponse])
def get_active_banners(user_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    """If user_id is provided, returns only banners targeted to that user.
    Otherwise returns all active banners (admin/global view)."""
    if user_id:
        return store.get_active_banners_for_user(db, user_id)
    cached = redis_manager.get_cached_banners()
    if cached is not None:
        return cached
    banners = store.get_active_banners(db)
    redis_manager.set_cached_banners(banners)
    return banners


@router.post("/notifications/banner")
def create_banner(request: CreateBannerRequest, user_id: int = Query(1), db: Session = Depends(get_db)):
    try:
        notif, recipient_ids = store.create_notification(
            db=db, title=request.title, message=request.message,
            delivery_mode="banner", domain_type=request.domain_type,
            visibility=request.visibility, priority=request.priority,
            target_type=request.target_type, target_id=request.target_id,
            source_service="system", metadata=request.metadata,
            created_by=user_id, expires_at=request.expires_at,
        )
    except TargetValidationError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})
    pub = {"id": notif.id, "title": notif.title, "message": notif.message,
           "priority": notif.priority, "domain_type": notif.domain_type,
           "visibility": notif.visibility, "target_type": notif.target_type,
           "expires_at": str(notif.expires_at) if notif.expires_at else None,
           "created_at": str(notif.created_at),
           "recipient_ids": list(recipient_ids)}
    redis_manager.publish_banner("create", pub)
    redis_manager.invalidate_banner_cache()
    # Publish full banner snapshot to each affected user
    snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
    redis_manager.publish_banner_snapshots(snapshots)
    return SendNotificationResponse(success=True, notification_id=notif.id,
                                    recipients_count=len(recipient_ids),
                                    message=f"Banner created for {len(recipient_ids)} users")


# --- Send notification ---

@router.post("/notifications/send")
def send_notification(request: SendNotificationRequest, user_id: int = Query(1), db: Session = Depends(get_db)):
    try:
        notif, recipient_ids = store.create_notification(
            db=db, title=request.title, message=request.message,
            delivery_mode=request.delivery_mode, domain_type=request.domain_type,
            visibility=request.visibility, priority=request.priority,
            target_type=request.target_type, target_id=request.target_id,
            source_service="system", metadata=request.metadata, created_by=user_id,
        )
    except TargetValidationError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})
    pub = {"id": notif.id, "title": notif.title, "message": notif.message,
           "delivery_mode": notif.delivery_mode, "domain_type": notif.domain_type,
           "visibility": notif.visibility, "priority": notif.priority,
           "source_service": "system", "metadata": request.metadata,
           "created_at": str(notif.created_at)}
    redis_manager.invalidate_unread_count(recipient_ids)
    unread_counts_by_mode = store.get_unread_counts_by_mode_bulk(db, recipient_ids)
    if notif.visibility == "public" or request.target_type == "all":
        redis_manager.publish_broadcast(pub, user_unread_counts=unread_counts_by_mode)
    else:
        redis_manager.publish_to_users(recipient_ids, pub, unread_counts=unread_counts_by_mode)
    return SendNotificationResponse(success=True, notification_id=notif.id,
                                    recipients_count=len(recipient_ids),
                                    message=f"Sent to {len(recipient_ids)} recipients")


# --- Admin logs ---

@router.get("/notifications/admin/logs")
def get_admin_logs(
    domain_type: Optional[str] = Query(None),
    visibility: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    source_service: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None, description="Comma-separated event names"),
    user_id: Optional[str] = Query(None, description="Comma-separated user IDs"),
    job_id: Optional[str] = Query(None, description="Comma-separated job IDs"),
    company_id: Optional[str] = Query(None, description="Comma-separated company IDs"),
    created_by: Optional[str] = Query(None, description="Comma-separated admin IDs"),
    delivery_mode: Optional[str] = Query(None, description="Comma-separated: push,banner,log"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
):
    parsed_from = None
    parsed_to = None
    if date_from:
        try:
            parsed_from = datetime.fromisoformat(date_from)
        except ValueError:
            pass
    if date_to:
        try:
            parsed_to = datetime.fromisoformat(date_to)
        except ValueError:
            pass
    results, total = store.get_admin_notification_logs(
        db=db, page=page, limit=limit, domain_type=domain_type, visibility=visibility,
        date_from=parsed_from, date_to=parsed_to, priority=priority,
        source_service=source_service, event_type=event_type,
        user_id=user_id, job_id=job_id, company_id=company_id,
        created_by=created_by, delivery_mode=delivery_mode,
        sort_by=sort_by, sort_order=sort_order,
    )
    total_pages = math.ceil(total / limit) if total > 0 else 1
    return AdminNotificationListResponse(
        notifications=[AdminNotificationOut(**r) for r in results],
        pagination=PaginationDetails(page=page, limit=limit, total_pages=total_pages, total_elements=total),
    )


# --- Admin Stats + Management (test, no auth) ---

@router.get("/notifications/admin/stats")
def test_admin_stats(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    parsed_from = datetime.fromisoformat(date_from) if date_from else None
    parsed_to = datetime.fromisoformat(date_to) if date_to else None
    return store.get_admin_stats(db=db, date_from=parsed_from, date_to=parsed_to)


@router.put("/notifications/admin/banners/{banner_id}/expiry")
def test_change_banner_expiry(banner_id: int, expires_at: Optional[str] = Query(None),
                               db: Session = Depends(get_db)):
    parsed = datetime.fromisoformat(expires_at) if expires_at else None
    banner = store.update_banner_expiry(db, banner_id, parsed)
    if not banner:
        raise HTTPException(status_code=404, detail="Banner not found")
    redis_manager.invalidate_banner_cache()
    if banner.is_active == 0:
        recipient_ids = store.get_banner_recipient_ids(db, banner_id)
        redis_manager.publish_banner("expire", {"id": banner_id, "recipient_ids": list(recipient_ids)})
        if recipient_ids:
            snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
            redis_manager.publish_banner_snapshots(snapshots)
    return {"success": True, "banner_id": banner_id, "is_active": bool(banner.is_active),
            "expires_at": banner.expires_at}


@router.put("/notifications/admin/banners/{banner_id}/expire-now")
def test_expire_banner_now(banner_id: int, db: Session = Depends(get_db)):
    result = store.expire_banner_now(db, banner_id)
    if not result:
        raise HTTPException(status_code=404, detail="Banner not found")
    banner, recipient_ids = result
    redis_manager.publish_banner("expire", {"id": banner_id, "recipient_ids": list(recipient_ids)})
    redis_manager.invalidate_banner_cache()
    if recipient_ids:
        snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
        redis_manager.publish_banner_snapshots(snapshots)
    return {"success": True, "banner_id": banner_id, "recipients_notified": len(recipient_ids)}


@router.delete("/notifications/admin/{notification_id}")
def test_delete_notification(notification_id: int, db: Session = Depends(get_db)):
    recipient_ids = store.get_notification_recipient_ids(db, notification_id)
    notif = store.soft_delete_notification(db, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")

    if notif.delivery_mode == "banner":
        redis_manager.publish_banner("expire", {"id": notification_id, "recipient_ids": list(recipient_ids)})
        redis_manager.invalidate_banner_cache()
        if recipient_ids:
            snapshots = store.get_active_banners_for_users_bulk(db, recipient_ids)
            redis_manager.publish_banner_snapshots(snapshots)

    if recipient_ids:
        redis_manager.invalidate_unread_count(recipient_ids)
        counts_by_mode = store.get_unread_counts_by_mode_bulk(db, recipient_ids)
        for uid, counts in counts_by_mode.items():
            redis_manager.publish_unread_count(uid, counts.get("push", 0), by_mode=counts)

    return {"success": True, "notification_id": notification_id,
            "delivery_mode": notif.delivery_mode, "recipients_notified": len(recipient_ids)}


# --- Schedules ---

@router.post("/notifications/schedule")
def create_schedule(request: CreateScheduleRequest, user_id: int = Query(1), db: Session = Depends(get_db)):
    # Validate target upfront — fail fast on invalid job_id/user_id/role
    try:
        store.resolve_target_user_ids(
            db, request.target_type, request.target_id,
            include_admins=(request.target_type != "all"),
        )
    except TargetValidationError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})

    sched = store.create_schedule(
        db=db, title=request.title, message=request.message,
        delivery_mode=request.delivery_mode, domain_type=request.domain_type,
        visibility=request.visibility, priority=request.priority,
        target_type=request.target_type, target_id=request.target_id,
        extra_metadata=json.dumps(request.metadata) if request.metadata else None,
        scheduled_at=request.scheduled_at, repeat_type=request.repeat_type,
        repeat_until=request.repeat_until, created_by=user_id,
    )
    return ScheduleOut.model_validate(sched)


@router.get("/notifications/schedules")
def list_schedules(page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db)):
    schedules, total = store.get_schedules(db=db, page=page, limit=limit)
    total_pages = math.ceil(total / limit) if total > 0 else 1
    return ScheduleListResponse(
        schedules=[ScheduleOut.model_validate(s) for s in schedules],
        pagination=PaginationDetails(page=page, limit=limit, total_pages=total_pages, total_elements=total),
    )


@router.put("/notifications/schedules/{schedule_id}/cancel")
def cancel_schedule(schedule_id: int, db: Session = Depends(get_db)):
    success = store.cancel_schedule(db, schedule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Schedule not found or already sent/cancelled")
    return MarkReadResponse(success=True, message="Schedule cancelled")


# --- Event trigger ---

@router.post("/notifications/event")
def trigger_event(request: EventTriggerRequest, db: Session = Depends(get_db)):
    success, notification_id, message = handle_event(db=db, event_name=request.event_name, data=request.data or {})
    if not success:
        raise HTTPException(status_code=404, detail=message)
    return EventTriggerResponse(success=True, notification_id=notification_id, message=message)


# --- WebSocket (no auth — uses user_id query param) ---

@router.websocket("/ws/notifications")
async def ws_notifications_test(websocket: WebSocket, user_id: int = 1):
    """Test WebSocket — no JWT, just user_id param."""
    import asyncio
    await websocket.accept()

    try:
        # Send initial unread counts (per-mode) + active banners snapshot
        db = next(get_db())
        try:
            by_mode = store.get_unread_counts_by_mode(db, user_id)
            active_banners = store.get_active_banners_for_user(db, user_id)
        finally:
            db.close()
        await websocket.send_json({
            "type": "unread_count",
            "data": {
                "count": by_mode["push"],
                "push": by_mode["push"],
                "banner": by_mode["banner"],
                "log": by_mode["log"],
                "total": by_mode["total"],
            },
        })
        await websocket.send_json({
            "type": "banners",
            "action": "snapshot",
            "data": [
                {
                    "id": b["id"],
                    "title": b["title"],
                    "message": b["message"],
                    "priority": b["priority"],
                    "domain_type": b["domain_type"],
                    "expires_at": str(b["expires_at"]) if b.get("expires_at") else None,
                    "created_at": str(b["created_at"]) if b.get("created_at") else None,
                }
                for b in active_banners
            ],
        })

        # Subscribe to Redis pub/sub for this user + broadcast + banner
        r = redis_manager.get_pubsub_redis()
        pubsub = r.pubsub()
        pubsub.subscribe(f"notif:user:{user_id}", "notif:broadcast", "notif:banner")

        async def redis_to_client():
            while True:
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and isinstance(msg.get("data"), str):
                    try:
                        payload = json.loads(msg["data"])
                        channel = msg.get("channel", "")
                        if channel == "notif:banner":
                            # Filter banner events: only forward if this user is a recipient
                            data_field = payload.get("data") if isinstance(payload, dict) else None
                            recipient_ids = None
                            if isinstance(data_field, dict):
                                recipient_ids = data_field.get("recipient_ids")
                            if recipient_ids and user_id not in recipient_ids:
                                # Not a recipient — skip
                                pass
                            else:
                                # Strip recipient_ids before forwarding (internal routing detail)
                                if isinstance(data_field, dict) and "recipient_ids" in data_field:
                                    forward_data = dict(data_field)
                                    forward_data.pop("recipient_ids", None)
                                    payload = dict(payload)
                                    payload["data"] = forward_data
                                await websocket.send_json(payload)
                        elif isinstance(payload, dict) and payload.get("_meta") == "banners_snapshot":
                            # Per-user banner snapshot (sent on banner create/expire)
                            await websocket.send_json({
                                "type": "banners",
                                "action": "snapshot",
                                "data": payload.get("data", []),
                            })
                        elif isinstance(payload, dict) and payload.get("_meta") == "unread_count":
                            data_out = {"count": payload.get("count", 0)}
                            for k in ("push", "banner", "log", "total"):
                                if k in payload:
                                    data_out[k] = payload[k]
                            await websocket.send_json({
                                "type": "unread_count",
                                "data": data_out,
                            })
                        else:
                            await websocket.send_json({"type": "notification", "data": payload})
                    except Exception:
                        pass
                await asyncio.sleep(0.1)

        async def client_to_server():
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                    action = data.get("action")

                    def _publish_fresh_for_ws(_db):
                        redis_manager.invalidate_unread_count([user_id])
                        by_mode = store.get_unread_counts_by_mode(_db, user_id)
                        redis_manager.set_cached_unread_count(user_id, by_mode["push"])
                        redis_manager.publish_unread_count(user_id, by_mode["push"], by_mode=by_mode)

                    if action == "mark_read":
                        nid = data.get("notification_id")
                        if nid:
                            db = next(get_db())
                            try:
                                store.mark_notification_read(db, nid, user_id)
                                _publish_fresh_for_ws(db)
                            finally:
                                db.close()
                    elif action == "mark_unread":
                        nid = data.get("notification_id")
                        if nid:
                            db = next(get_db())
                            try:
                                store.mark_notification_unread(db, nid, user_id)
                                _publish_fresh_for_ws(db)
                            finally:
                                db.close()
                    elif action == "mark_all_read":
                        db = next(get_db())
                        try:
                            store.mark_all_read(db, user_id)
                            _publish_fresh_for_ws(db)
                        finally:
                            db.close()
                    elif action == "ping":
                        await websocket.send_json({"type": "pong"})
                except Exception:
                    pass

        await asyncio.gather(redis_to_client(), client_to_server())

    except WebSocketDisconnect:
        logger.info("Test WS disconnected: user_id=%s", user_id)
    except Exception as e:
        logger.error("Test WS error user_id=%s: %s", user_id, e)
    finally:
        try:
            pubsub.unsubscribe()
        except Exception:
            pass
