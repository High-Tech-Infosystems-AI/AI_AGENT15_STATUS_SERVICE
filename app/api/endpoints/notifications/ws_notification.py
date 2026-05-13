"""
WebSocket Notification Endpoint.
WS /ws/notifications?token=<jwt_token>

Real-time push stream for:
- Per-user notifications
- Broadcast notifications
- Banner create/expire events
- Unread count updates
"""

import logging
import json
from fastapi import WebSocket, WebSocketDisconnect, APIRouter, Query

from app.notification_layer.ws_manager import ws_manager
from app.notification_layer import redis_manager, store
from app.database_Layer.db_config import SessionLocal

logger = logging.getLogger("app_logger")
router = APIRouter()


async def _validate_ws_token(token: str) -> dict:
    """Validate JWT token for WebSocket connection (async + Redis-cached).
    Reuses the same cache as REST auth_utils.validate_token.
    """
    import hashlib, json as _json
    import httpx
    from app.core import settings
    from app.notification_layer import redis_manager

    cache_key = "auth:token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]

    # Cache lookup
    try:
        cached = redis_manager.get_notification_redis().get(cache_key)
        if cached:
            info = _json.loads(cached)
            return info
    except Exception:
        pass

    # Auth service call (async, non-blocking)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{settings.AUTH_SERVICE_URL}",
                params={"token": token},
                headers={"accept": "application/json"},
            )
        if response.status_code != 200:
            return None
        info = response.json()
        if not info.get("user_id"):
            return None
        # Cache for future requests
        try:
            redis_manager.get_notification_redis().setex(cache_key, 60, _json.dumps({
                "user_id": info["user_id"],
                "role_id": info.get("role_id"),
                "role_name": info.get("role_name"),
            }))
        except Exception:
            pass
        return info
    except Exception as e:
        logger.error("WS token validation error: %s", e)
        return None


@router.websocket("/ws/notifications")
async def ws_notifications(websocket: WebSocket, token: str = Query(...)):
    """
    WebSocket endpoint for real-time notification streaming.

    On connect:
    1. Validate JWT from query parameter
    2. Register connection with ws_manager
    3. Send initial unread count
    4. Listen for client messages (mark_read, ping)

    Notifications are delivered via Redis Pub/Sub → ws_manager fan-out.
    """
    # Validate token
    user_info = await _validate_ws_token(token)
    if not user_info:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    user_id = user_info["user_id"]

    # Accept and register
    await ws_manager.connect(websocket, user_id)

    try:
        # Send initial unread counts (per-mode) + active banners snapshot
        db = SessionLocal()
        try:
            by_mode = store.get_unread_counts_by_mode(db, user_id)
            redis_manager.set_cached_unread_count(user_id, by_mode["push"])

            # Send active banners snapshot — only banners this user is a recipient of
            active_banners = store.get_active_banners_for_user(db, user_id)
        finally:
            db.close()

        await websocket.send_json({
            "type": "unread_count",
            "data": {
                "count": by_mode["push"],
                "push": by_mode["push"],
                "banner": by_mode["banner"],
                "log": by_mode["log"],
                "total": by_mode["total"],
            },
        })
        await websocket.send_json({
            "type": "banners",
            "action": "snapshot",
            "data": [
                {
                    "id": b["id"],
                    "title": b["title"],
                    "message": b["message"],
                    "priority": b["priority"],
                    "domain_type": b["domain_type"],
                    "expires_at": str(b["expires_at"]) if b.get("expires_at") else None,
                    "created_at": str(b["created_at"]) if b.get("created_at") else None,
                }
                for b in active_banners
            ],
        })

        # Listen for client messages
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            action = msg.get("action")

            def _publish_fresh():
                """Recompute per-mode counts and publish to user's WS channel."""
                redis_manager.invalidate_unread_count([user_id])
                by_mode = store.get_unread_counts_by_mode(db, user_id)
                redis_manager.set_cached_unread_count(user_id, by_mode["push"])
                redis_manager.publish_unread_count(user_id, by_mode["push"], by_mode=by_mode)

            if action == "mark_read":
                notification_id = msg.get("notification_id")
                if notification_id:
                    db = SessionLocal()
                    try:
                        store.mark_notification_read(db, notification_id, user_id)
                        _publish_fresh()
                    finally:
                        db.close()

            elif action == "mark_unread":
                notification_id = msg.get("notification_id")
                if notification_id:
                    db = SessionLocal()
                    try:
                        store.mark_notification_unread(db, notification_id, user_id)
                        _publish_fresh()
                    finally:
                        db.close()

            elif action == "mark_all_read":
                db = SessionLocal()
                try:
                    store.mark_all_read(db, user_id)
                    _publish_fresh()
                finally:
                    db.close()

            elif action == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info("WS notification disconnected: user_id=%s", user_id)
    except Exception as e:
        logger.error("WS notification error for user %s: %s", user_id, e)
    finally:
        ws_manager.disconnect(websocket, user_id)
