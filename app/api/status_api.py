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
]

# Include WebSocket router
router.include_router(websocket_tasks.router, tags=["WebSocket"])

# Include REST API endpoints
router.include_router(get_running_resume.router, tags=["Resume"])
router.include_router(get_running_summaries.router, tags=["Summaries"])
router.include_router(get_running_matcher.router, tags=["Matcher"])

