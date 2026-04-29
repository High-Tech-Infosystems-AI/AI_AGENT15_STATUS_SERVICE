"""Aggregator router for the AI chatbot service."""
from fastapi import APIRouter

from app.ai_chat_layer.api import (
    anomaly_api, approvals_api, audit_api, messages_api,
    quota_api, schedules_api,
)

router = APIRouter()
router.include_router(messages_api.router, tags=["AI Chat - Messages"])
router.include_router(quota_api.router, tags=["AI Chat - Quota"])
router.include_router(audit_api.router, tags=["AI Chat - Audit"])
router.include_router(schedules_api.router, tags=["AI Chat - Schedules"])
router.include_router(anomaly_api.router, tags=["AI Chat - Anomaly"])
router.include_router(approvals_api.router, tags=["AI Chat - Approvals"])
