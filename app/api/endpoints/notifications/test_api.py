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
    cached = redis_manager.get_cached_unread_count(user_id)
    if cached is not None:
        return UnreadCountResponse(count=cached)
    count = store.get_unread_count(db, user_id)
    redis_manager.set_cached_unread_count(user_id, count)
    return UnreadCountResponse(count=count)


@router.put("/notifications/{notification_id}/read")
def mark_read(notification_id: int, user_id: int = Query(1), db: Session = Depends(get_db)):
    success = store.mark_notification_read(db, notification_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found for this user")
    redis_manager.invalidate_unread_count([user_id])
    return MarkReadResponse(success=True, message="Notification marked as read")


@router.put("/notifications/mark-all-read")
def mark_all_read(user_id: int = Query(1), db: Session = Depends(get_db)):
    count = store.mark_all_read(db, user_id)
    redis_manager.invalidate_unread_count([user_id])
    return MarkReadResponse(success=True, message=f"Marked {count} notifications as read")


# --- Banners ---

@router.get("/notifications/banners/active", response_model=List[BannerResponse])
def get_active_banners(db: Session = Depends(get_db)):
    cached = redis_manager.get_cached_banners()
    if cached is not None:
        return cached
    banners = store.get_active_banners(db)
    redis_manager.set_cached_banners(banners)
    return banners


@router.post("/notifications/banner")
def create_banner(request: CreateBannerRequest, user_id: int = Query(1), db: Session = Depends(get_db)):
    notif, recipient_ids = store.create_notification(
        db=db, title=request.title, message=request.message,
        delivery_mode="banner", domain_type=request.domain_type,
        visibility="public", priority=request.priority,
        target_type="all", target_id=None,
        source_service="system", metadata=request.metadata,
        created_by=user_id, expires_at=request.expires_at,
    )
    pub = {"id": notif.id, "title": notif.title, "message": notif.message,
           "priority": notif.priority, "domain_type": notif.domain_type,
           "expires_at": str(notif.expires_at) if notif.expires_at else None,
           "created_at": str(notif.created_at)}
    redis_manager.publish_banner("create", pub)
    redis_manager.invalidate_banner_cache()
    redis_manager.invalidate_unread_count(recipient_ids)
    return SendNotificationResponse(success=True, notification_id=notif.id,
                                    recipients_count=len(recipient_ids),
                                    message=f"Banner created for {len(recipient_ids)} users")


# --- Send notification ---

@router.post("/notifications/send")
def send_notification(request: SendNotificationRequest, user_id: int = Query(1), db: Session = Depends(get_db)):
    notif, recipient_ids = store.create_notification(
        db=db, title=request.title, message=request.message,
        delivery_mode=request.delivery_mode, domain_type=request.domain_type,
        visibility=request.visibility, priority=request.priority,
        target_type=request.target_type, target_id=request.target_id,
        source_service="system", metadata=request.metadata, created_by=user_id,
    )
    pub = {"id": notif.id, "title": notif.title, "message": notif.message,
           "delivery_mode": notif.delivery_mode, "domain_type": notif.domain_type,
           "visibility": notif.visibility, "priority": notif.priority,
           "source_service": "system", "metadata": request.metadata,
           "created_at": str(notif.created_at)}
    if notif.visibility == "public" or request.target_type == "all":
        redis_manager.publish_broadcast(pub)
    else:
        redis_manager.publish_to_users(recipient_ids, pub)
    redis_manager.invalidate_unread_count(recipient_ids)
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
    event_type: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    created_by: Optional[int] = Query(None),
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
    results, total = store.get_admin_notification_logs(
        db=db, page=page, limit=limit, domain_type=domain_type, visibility=visibility,
        date_from=parsed_from, date_to=parsed_to, priority=priority,
        source_service=source_service, event_type=event_type,
        user_id=user_id, created_by=created_by, delivery_mode=delivery_mode,
        sort_by=sort_by, sort_order=sort_order,
    )
    total_pages = math.ceil(total / limit) if total > 0 else 1
    return AdminNotificationListResponse(
        notifications=[AdminNotificationOut(**r) for r in results],
        pagination=PaginationDetails(page=page, limit=limit, total_pages=total_pages, total_elements=total),
    )


# --- Schedules ---

@router.post("/notifications/schedule")
def create_schedule(request: CreateScheduleRequest, user_id: int = Query(1), db: Session = Depends(get_db)):
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
        # Send initial unread count
        db = next(get_db())
        try:
            count = store.get_unread_count(db, user_id)
        finally:
            db.close()
        await websocket.send_json({"type": "unread_count", "data": {"count": count}})

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
                            await websocket.send_json(payload)
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
                    if data.get("action") == "mark_read":
                        nid = data.get("notification_id")
                        if nid:
                            db = next(get_db())
                            try:
                                store.mark_notification_read(db, nid, user_id)
                                redis_manager.invalidate_unread_count([user_id])
                                new_count = store.get_unread_count(db, user_id)
                                await websocket.send_json({"type": "unread_count", "data": {"count": new_count}})
                            finally:
                                db.close()
                    elif data.get("action") == "ping":
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
