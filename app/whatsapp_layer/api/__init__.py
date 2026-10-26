"""Aggregate routers — mounted by app.main:

  whatsapp_router              → /status/whatsapp
  clawback_notifications_router → /status/clawback-notifications
"""
from fastapi import APIRouter

from app.whatsapp_layer.api import (
    health_checks_api, logs_api, provider_api, rules_api, templates_api, webhook_api,
)

whatsapp_router = APIRouter()
whatsapp_router.include_router(provider_api.router, tags=["WhatsApp - Accounts"])
whatsapp_router.include_router(templates_api.router, tags=["WhatsApp - Templates"])
whatsapp_router.include_router(webhook_api.router, tags=["WhatsApp - Webhooks"])

clawback_notifications_router = APIRouter()
clawback_notifications_router.include_router(rules_api.router, tags=["Clawback Notifications - Rules"])
clawback_notifications_router.include_router(logs_api.router, tags=["Clawback Notifications - Logs"])
clawback_notifications_router.include_router(health_checks_api.router,
                                             tags=["Clawback Notifications - Health Checks"])
