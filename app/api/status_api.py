"""
Status API Router

Main router for status service API endpoints including WebSocket support.
"""

from fastapi import APIRouter
from app.api.endpoints import websocket_tasks, get_running_resume, get_running_summaries, get_running_matcher

router = APIRouter()

# Public paths used by API gateway / Consul for no-jwt tagging.
NO_AUTH_PATHS = [
    "/health",
    "/status/health",
    "/model/api/docs",
    "/openapi.json",
    "/redoc",
    "/ws/tasks/{task_id}",
    "/status/ws/tasks/{task_id}",
    "/ws/notifications",
    "/status/ws/notifications",
    "/status/notifications/ws/notifications",
    "/test",
    "/test/",
    "/test/users",
    "/test/notifications",
    "/test/notifications/unread-count",
    "/test/notifications/banners/active",
    "/test/notifications/admin/logs",
    "/test/notifications/send",
    "/test/notifications/banner",
    "/test/notifications/event",
    "/test/notifications/schedule",
    "/test/notifications/schedules",
    "/test/notifications/mark-all-read",
    "/test/ws/notifications",
    "/test/debug/notifications",
    "/test/debug/recipients",
    "/status/test",
    "/status/test/ws/notifications",
]

# Include WebSocket router
router.include_router(websocket_tasks.router, tags=["WebSocket"])

# Include REST API endpoints
router.include_router(get_running_resume.router, tags=["Resume"])
router.include_router(get_running_summaries.router, tags=["Summaries"])
router.include_router(get_running_matcher.router, tags=["Matcher"])

