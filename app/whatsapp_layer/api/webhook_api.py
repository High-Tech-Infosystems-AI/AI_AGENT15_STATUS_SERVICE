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
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.whatsapp_layer import webhooks

logger = logging.getLogger("app_logger")
router = APIRouter()

MAX_BODY_BYTES = 1024 * 1024


def _token_account(db: Session, request: Request):
    """BroadNet keeps the URL and token apart, so the token may arrive in a
    header or query parameter instead of the path."""
    for value in [*request.query_params.values(), *request.headers.values()]:
        value = value.strip()
        value = value[7:].strip() if value.lower().startswith("bearer ") else value
        if 20 <= len(value) <= 128:
            account = webhooks.find_account(db, "omnichannel", value)
            if account is not None:
                return account
    return None


@router.get("/webhooks/omnichannel")
async def omnichannel_webhook_verify(request: Request, db: Session = Depends(get_db)):
    """Meta-style verification: echo hub.challenge for the right token."""
    account = await run_in_threadpool(_token_account, db, request)
    if account is None:
        raise HTTPException(404, "Not found")
    return PlainTextResponse(request.query_params.get("hub.challenge", "ok"))


@router.post("/webhooks/omnichannel")
async def omnichannel_webhook_no_path_token(request: Request, db: Session = Depends(get_db)):
    account = await run_in_threadpool(_token_account, db, request)
    if account is None:
        # Names only: tells us how the provider sends its token, leaks no values.
        logger.warning("Webhook without a known token; query keys %s, header names %s",
                       list(request.query_params.keys()), list(request.headers.keys()))
        raise HTTPException(404, "Not found")
    raw = (await request.body())[:MAX_BODY_BYTES]
    headers = {k: v for k, v in request.headers.items()}
    await run_in_threadpool(webhooks.process, db, account, headers, raw)
    return {"received": True}


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
