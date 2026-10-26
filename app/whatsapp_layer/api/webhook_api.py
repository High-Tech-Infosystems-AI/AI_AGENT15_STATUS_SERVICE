"""Provider webhooks — POST /status/whatsapp/webhooks/omnichannel/{webhook_token}
(contract §2.4).

No JWT: the gateway passes /status/whatsapp/webhooks/ through (Consul
no_auth_path); the random per-account token in the path identifies the
account. Always 200 {"received": true}; 404 for an unknown token.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.whatsapp_layer import webhooks

logger = logging.getLogger("app_logger")
router = APIRouter()

MAX_BODY_BYTES = 1024 * 1024


@router.post("/webhooks/omnichannel/{webhook_token}")
async def omnichannel_webhook(webhook_token: str, request: Request,
                              db: Session = Depends(get_db)):
    account = await run_in_threadpool(webhooks.find_account, db, "omnichannel", webhook_token)
    if account is None:
        raise HTTPException(404, "Not found")
    raw = (await request.body())[:MAX_BODY_BYTES]
    headers = {k: v for k, v in request.headers.items()}
    # Storing + mapping is blocking DB work — keep it off the event loop.
    await run_in_threadpool(webhooks.process, db, account, headers, raw)
    return {"received": True}
