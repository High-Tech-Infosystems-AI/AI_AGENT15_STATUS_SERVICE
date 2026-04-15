"""
Notification UI Server — port 5009.

REST proxy to Status Service /test/* endpoints.
WebSocket handled directly via Redis pub/sub (no proxy).
"""

import os
import json
import asyncio
import logging
from pathlib import Path

import httpx
import redis
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.WARNING)
logging.getLogger("notification_ui").setLevel(logging.INFO)
# Silence noisy httpx request logging
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("notification_ui")

STATUS_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8515")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "") or None
UI_PORT = int(os.getenv("UI_PORT", "5009"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Notification Test UI", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# --- REST Proxy: /api/* → Status Service /test/* ---
# Each request gets its own short-lived client to avoid pool exhaustion.

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_api(request: Request, path: str):
    target = f"{STATUS_SERVICE_URL}/test/{path}"
    params = dict(request.query_params)
    headers = {"Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if request.method == "GET":
                resp = await client.get(target, params=params, headers=headers)
            elif request.method == "POST":
                body = await request.body()
                resp = await client.post(target, content=body, params=params, headers=headers)
            elif request.method == "PUT":
                body = await request.body()
                resp = await client.put(target, content=body, params=params, headers=headers)
            elif request.method == "DELETE":
                resp = await client.delete(target, params=params, headers=headers)
            else:
                return JSONResponse({"error": "Method not allowed"}, status_code=405)

            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            return JSONResponse(data, status_code=resp.status_code)
    except Exception as e:
        logger.error("Proxy error: %s → %s", target, e)
        return JSONResponse({"error": str(e)}, status_code=502)


# --- WebSocket: direct Redis pub/sub (no HTTP proxy, no pool issues) ---

def _get_redis_pubsub():
    """Create a fresh Redis connection for pub/sub."""
    r = redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
        password=REDIS_PASSWORD, decode_responses=True,
        socket_connect_timeout=5, socket_timeout=2,
    )
    return r.pubsub()


@app.websocket("/ws/notifications")
async def ws_notifications(websocket: WebSocket, user_id: int = 1):
    await websocket.accept()
    logger.info("WS connected: user_id=%s", user_id)

    pubsub = None
    try:
        # Send initial unread count + active banners snapshot
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                # Unread count
                resp = await c.get(
                    f"{STATUS_SERVICE_URL}/test/notifications/unread-count",
                    params={"user_id": user_id},
                )
                if resp.status_code == 200:
                    await websocket.send_json(resp.json())

                # Active banners snapshot — per-user
                banners_resp = await c.get(
                    f"{STATUS_SERVICE_URL}/test/notifications/banners/active",
                    params={"user_id": user_id},
                )
                if banners_resp.status_code == 200:
                    banners = banners_resp.json() or []
                    await websocket.send_json({
                        "type": "banners",
                        "action": "snapshot",
                        "data": banners,
                    })
        except Exception as e:
            logger.warning("Failed to send WS initial snapshot: %s", e)

        # Subscribe to Redis pub/sub
        pubsub = _get_redis_pubsub()
        pubsub.subscribe(f"notif:user:{user_id}", "notif:broadcast", "notif:banner")

        async def redis_to_client():
            while True:
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                if msg and isinstance(msg.get("data"), str):
                    try:
                        payload = json.loads(msg["data"])
                        channel = msg.get("channel", "")
                        if channel == "notif:banner":
                            # Filter by recipient_ids if present
                            data_field = payload.get("data") if isinstance(payload, dict) else None
                            recipient_ids = None
                            if isinstance(data_field, dict):
                                recipient_ids = data_field.get("recipient_ids")
                            if recipient_ids and user_id not in recipient_ids:
                                pass  # not for this user
                            else:
                                if isinstance(data_field, dict) and "recipient_ids" in data_field:
                                    forward_data = dict(data_field)
                                    forward_data.pop("recipient_ids", None)
                                    payload = dict(payload)
                                    payload["data"] = forward_data
                                await websocket.send_json(payload)
                        elif isinstance(payload, dict) and payload.get("_meta") == "unread_count":
                            data_out = {"count": payload.get("count", 0)}
                            for k in ("push", "banner", "log", "total"):
                                if k in payload:
                                    data_out[k] = payload[k]
                            await websocket.send_json({
                                "type": "unread_count",
                                "data": data_out,
                            })
                        elif isinstance(payload, dict) and payload.get("_meta") == "banners_snapshot":
                            # Per-user banner snapshot (sent on banner create/expire)
                            await websocket.send_json({
                                "type": "banners",
                                "action": "snapshot",
                                "data": payload.get("data", []),
                            })
                        else:
                            # Notification — may include unread_count inline
                            await websocket.send_json({"type": "notification", "data": payload})
                            if isinstance(payload, dict) and "unread_count" in payload:
                                await websocket.send_json({
                                    "type": "unread_count",
                                    "data": {"count": payload["unread_count"]},
                                })
                    except Exception:
                        pass
                await asyncio.sleep(0.1)

        async def client_to_server():
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                    action = data.get("action")
                    if action == "mark_read":
                        nid = data.get("notification_id")
                        if nid:
                            try:
                                async with httpx.AsyncClient(timeout=5) as c:
                                    await c.put(
                                        f"{STATUS_SERVICE_URL}/test/notifications/{nid}/read",
                                        params={"user_id": user_id},
                                    )
                                    resp = await c.get(
                                        f"{STATUS_SERVICE_URL}/test/notifications/unread-count",
                                        params={"user_id": user_id},
                                    )
                                    if resp.status_code == 200:
                                        await websocket.send_json(resp.json())
                            except Exception:
                                pass
                    elif action == "ping":
                        await websocket.send_json({"type": "pong"})
                except Exception:
                    pass

        await asyncio.gather(redis_to_client(), client_to_server())

    except WebSocketDisconnect:
        logger.info("WS disconnected: user_id=%s", user_id)
    except Exception as e:
        logger.error("WS error user_id=%s: %s", user_id, e)
    finally:
        if pubsub:
            try:
                pubsub.unsubscribe()
                pubsub.close()
            except Exception:
                pass


# --- Static ---

@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notification-ui"}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=UI_PORT)
