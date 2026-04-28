"""Web Push subscription endpoints.

Frontend flow:
  1. GET  /chat/push/vapid-public-key       → VAPID public key (b64url)
  2. SW registered by the frontend, PushManager.subscribe(...) returns a
     PushSubscription containing endpoint + keys.
  3. POST /chat/push/subscribe              → register that subscription
  4. POST /chat/push/test (optional)        → server pushes a test event so
                                              the UI can verify SW + perms
  5. POST /chat/push/unsubscribe            → drop the subscription on logout
                                              or "disable notifications"
"""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.chat_layer import push_service
from app.chat_layer.auth import current_user
from app.database_Layer.db_config import SessionLocal

router = APIRouter()


class _SubscriptionKeys(BaseModel):
    p256dh: str = Field(..., min_length=1)
    auth: str = Field(..., min_length=1)


class SubscribeRequest(BaseModel):
    """Mirrors the shape returned by PushSubscription.toJSON() in the browser
    so the frontend can pass it through verbatim."""
    endpoint: str = Field(..., min_length=1, max_length=2048)
    keys: _SubscriptionKeys


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2048)


@router.get("/push/vapid-public-key")
def get_vapid_public_key():
    """Public-key bootstrap for the frontend. Stable across restarts as long
    as VAPID_KEYS_FILE survives or env vars are set."""
    return {"public_key": push_service.vapid_public_key_b64url()}


@router.post("/push/subscribe")
def subscribe(req: SubscribeRequest, request: Request,
              user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        ua = request.headers.get("user-agent")
        sub = push_service.upsert_subscription(
            db, user_id=user["user_id"], endpoint=req.endpoint,
            p256dh=req.keys.p256dh, auth=req.keys.auth, user_agent=ua,
        )
        return {"id": sub.id, "endpoint": sub.endpoint}
    finally:
        db.close()


@router.post("/push/unsubscribe")
def unsubscribe(req: UnsubscribeRequest, _user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        n = push_service.delete_subscription_by_endpoint(db, req.endpoint)
        return {"deleted": n}
    finally:
        db.close()


class TestPushRequest(BaseModel):
    title: Optional[str] = "Test notification"
    body: Optional[str] = "If you're seeing this, web push is working."


@router.post("/push/test")
def send_test_push(req: TestPushRequest, user: dict = Depends(current_user)):
    """Push a payload to the caller. Useful for the UI to verify SW + perms
    after subscribe — and for ops to debug a particular user's setup."""
    db = SessionLocal()
    try:
        stats = push_service.send_to_user(db, user["user_id"], {
            "title": req.title or "Test notification",
            "body": req.body or "",
            "metadata": {"link": "/chat"},
        })
        if stats["total_subscriptions"] == 0:
            return JSONResponse(status_code=404, content={
                "error_code": "PUSH_NO_SUBSCRIPTIONS",
                "message": "No active push subscriptions for this user",
            })
        return {"status": "ok", **stats}
    finally:
        db.close()
