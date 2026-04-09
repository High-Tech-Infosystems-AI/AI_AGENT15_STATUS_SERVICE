"""
Notification UI Server — port 5009.

Proxies REST calls to Status Service /test/* endpoints.
WebSocket handled directly here (subscribes to Redis pub/sub — same Redis, no proxy needed).
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification_ui")

STATUS_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8515")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "") or None
UI_PORT = int(os.getenv("UI_PORT", "5009"))
STATIC_DIR = Path(__file__).parent / "static"

# Shared httpx client (reused across requests — avoids connection pool exhaustion)
_http_client = None


def get_http_client():
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=20))
    return _http_client


app = FastAPI(title="Notification Test UI", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.on_event("shutdown")
async def shutdown():
    if _http_client:
        await _http_client.aclose()


# --- REST Proxy: /api/* → Status Service /test/* ---

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_api(request: Request, path: str):
    target = f"{STATUS_SERVICE_URL}/test/{path}"
    params = dict(request.query_params)
    headers = {"Content-Type": "application/json"}
    client = get_http_client()

    try:
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


# --- WebSocket: handled directly (no proxy, subscribes to Redis pub/sub) ---

@app.websocket("/ws/notifications")
async def ws_notifications(websocket: WebSocket, user_id: int = 1):
    """
    Direct WebSocket — subscribes to Redis pub/sub for the selected user.
    No proxy to the status service needed since we share the same Redis.
    """
    await websocket.accept()
    logger.info("UI WS connected: user_id=%s", user_id)

    pubsub = None
    try:
        # Get unread count via REST (simple, avoids DB dependency in UI server)
        client = get_http_client()
        try:
            resp = await client.get(
                f"{STATUS_SERVICE_URL}/test/notifications/unread-count",
                params={"user_id": user_id},
            )
            if resp.status_code == 200:
                await websocket.send_json(resp.json())
        except Exception as e:
            logger.warning("Failed to get initial unread count: %s", e)

        # Subscribe to Redis pub/sub channels for this user
        r = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
            password=REDIS_PASSWORD, decode_responses=True,
            socket_connect_timeout=5, socket_timeout=5,
        )
        pubsub = r.pubsub()
        pubsub.subscribe(f"notif:user:{user_id}", "notif:broadcast", "notif:banner")
        logger.info("UI WS subscribed to Redis for user_id=%s", user_id)

        async def redis_listener():
            """Poll Redis pub/sub and forward messages to the WebSocket client."""
            while True:
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                if msg and isinstance(msg.get("data"), str):
                    try:
                        payload = json.loads(msg["data"])
                        channel = msg.get("channel", "")
                        if channel == "notif:banner":
                            await websocket.send_json(payload)
                        else:
                            await websocket.send_json({"type": "notification", "data": payload})
                        # Also send updated unread count
                        try:
                            resp = await client.get(
                                f"{STATUS_SERVICE_URL}/test/notifications/unread-count",
                                params={"user_id": user_id},
                            )
                            if resp.status_code == 200:
                                count_data = resp.json()
                                await websocket.send_json({"type": "unread_count", "data": {"count": count_data.get("count", 0)}})
                        except Exception:
                            pass
                    except Exception as e:
                        logger.warning("Redis→WS error: %s", e)
                await asyncio.sleep(0.1)

        async def client_listener():
            """Listen for client messages (mark_read, ping)."""
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                    action = data.get("action")
                    if action == "mark_read":
                        nid = data.get("notification_id")
                        if nid:
                            try:
                                await client.put(
                                    f"{STATUS_SERVICE_URL}/test/notifications/{nid}/read",
                                    params={"user_id": user_id},
                                )
                                resp = await client.get(
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

        await asyncio.gather(redis_listener(), client_listener())

    except WebSocketDisconnect:
        logger.info("UI WS disconnected: user_id=%s", user_id)
    except Exception as e:
        logger.error("UI WS error user_id=%s: %s", user_id, e)
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
