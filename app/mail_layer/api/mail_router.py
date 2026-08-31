"""Aggregate mail APIRouter — mounted at /mail by app.mail_main."""
from fastapi import APIRouter

from app.mail_layer.api import (
    accounts_api, attachments_api, compose_api, folders_api, messages_api,
    settings_api,
)

router = APIRouter()
router.include_router(accounts_api.router, tags=["Mail - Accounts"])
router.include_router(folders_api.router, tags=["Mail - Folders"])
router.include_router(messages_api.router, tags=["Mail - Messages"])
router.include_router(compose_api.router, tags=["Mail - Compose"])
router.include_router(attachments_api.router, tags=["Mail - Attachments"])
router.include_router(settings_api.router, tags=["Mail - Settings"])
