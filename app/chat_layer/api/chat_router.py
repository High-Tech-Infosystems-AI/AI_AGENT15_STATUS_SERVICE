"""Aggregate chat APIRouter."""
from fastapi import APIRouter

from app.chat_layer.api import (
    attachments_api, conversations_api, messages_api,
    presence_api, push_api, search_api, ws_chat,
)

router = APIRouter()
router.include_router(conversations_api.router, tags=["Chat - Conversations"])
router.include_router(messages_api.router, tags=["Chat - Messages"])
router.include_router(attachments_api.router, tags=["Chat - Attachments"])
router.include_router(presence_api.router, tags=["Chat - Presence"])
router.include_router(search_api.router, tags=["Chat - Search"])
router.include_router(push_api.router, tags=["Chat - Web Push"])
router.include_router(ws_chat.router, tags=["Chat - WebSocket"])
