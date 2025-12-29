"""
Status API Router

Main router for status service API endpoints including WebSocket support.
"""

from fastapi import APIRouter
from app.api.endpoints import websocket_tasks, get_running_resume, get_running_summaries

router = APIRouter()

# Include WebSocket router
router.include_router(websocket_tasks.router, tags=["WebSocket"])

# Include REST API endpoints
router.include_router(get_running_resume.router, tags=["Resume"])
router.include_router(get_running_summaries.router, tags=["Summaries"])

