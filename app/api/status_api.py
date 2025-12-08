"""
Status API Router

Main router for status service API endpoints including WebSocket support.
"""

from fastapi import APIRouter
from app.api.endpoints import websocket_tasks

router = APIRouter()

# Include WebSocket router
router.include_router(websocket_tasks.router, tags=["WebSocket"])

