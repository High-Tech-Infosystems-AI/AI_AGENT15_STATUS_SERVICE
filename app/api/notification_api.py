"""
Notification API Router — aggregates all notification endpoint modules.
"""

from fastapi import APIRouter
from app.api.endpoints.notifications import (
    send_notification_api,
    get_notifications_api,
    banner_api,
    notification_actions_api,
    admin_notifications_api,
    schedule_api,
    event_trigger_api,
    ws_notification,
)

router = APIRouter()

# Public paths that don't require JWT (WebSocket uses token query param)
NOTIFICATION_NO_AUTH_PATHS = [
    "/ws/notifications",
    "/status/ws/notifications",
]

# Manual triggers (admin only)
router.include_router(send_notification_api.router, tags=["Notifications - Send"])
router.include_router(banner_api.router, tags=["Notifications - Banners"])

# User notification log
router.include_router(get_notifications_api.router, tags=["Notifications - User Log"])

# Actions (read/unread)
router.include_router(notification_actions_api.router, tags=["Notifications - Actions"])

# Admin log
router.include_router(admin_notifications_api.router, tags=["Notifications - Admin Log"])

# Scheduling
router.include_router(schedule_api.router, tags=["Notifications - Scheduling"])

# Internal event trigger
router.include_router(event_trigger_api.router, tags=["Notifications - Events"])

# WebSocket
router.include_router(ws_notification.router, tags=["Notifications - WebSocket"])
