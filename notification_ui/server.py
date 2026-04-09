"""
Notification UI Server — lightweight FastAPI app on port 5009.

- Serves the static frontend (index.html)
- Proxies /auth/* to Login Service for authentication
- Proxies /api/notifications/* to Status/Notification Service
- Proxies /ws/notifications to Status/Notification Service WebSocket
- Proxies /api/users to RBAC/Login Service for user list
- Has a hardcoded test login for convenience
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta

import httpx
import jwt
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification_ui")

# --- Config ---
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://localhost:8085")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8515")
JWT_SECRET = os.getenv("JWT_SECRET_KEY", "h7ahasye8172#as819adh1COD797mTdAAA")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
UI_PORT = int(os.getenv("UI_PORT", "5009"))

# Hardcoded test credentials
TEST_USERNAME = os.getenv("TEST_USERNAME", "supriyohti")
TEST_PASSWORD = os.getenv("TEST_PASSWORD", "891y29hdfabsf8128")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Notification Test UI", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Auth endpoints ---

@app.post("/auth/login")
async def login(request: Request):
    """
    Login endpoint. First tries the hardcoded test user,
    then falls back to the real Auth Service.
    """
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")

    # Check hardcoded test user
    if username == TEST_USERNAME and password == TEST_PASSWORD:
        # Generate a JWT token that the notification service will accept
        payload = {
            "sub": f"test-session-{username}",
            "exp": datetime.utcnow() + timedelta(hours=24),
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        return JSONResponse({
            "token": token,
            "user_id": 1,
            "role_name": "super_admin",
            "username": username,
        })

    # Fallback: proxy to real auth service
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{AUTH_SERVICE_URL}/ats/login",
                json={"username": username, "password": password},
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("access_token"):
                return JSONResponse({
                    "token": data["access_token"],
                    "user_id": data.get("user_id"),
                    "role_name": data.get("role_name"),
                    "username": username,
                })
            return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    except Exception as e:
        logger.warning("Auth service unavailable: %s", e)
        return JSONResponse({"error": "Auth service unavailable"}, status_code=503)


# --- User list (for the dropdown) ---

@app.get("/api/users")
async def get_users(request: Request):
    """Proxy user list from Auth/RBAC service."""
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{AUTH_SERVICE_URL}/ats/get_all_user",
                params={"token": token},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                users = resp.json()
                return JSONResponse({"users": users})
    except Exception as e:
        logger.warning("Failed to fetch users: %s", e)
    return JSONResponse({"users": []})


# --- Notification API proxy ---

@app.api_route("/api/notifications/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_notifications(request: Request, path: str):
    """Proxy all /api/notifications/* to the Notification Service."""
    target_url = f"{NOTIFICATION_SERVICE_URL}/status/notifications/{path}"
    return await _proxy_request(request, target_url)


@app.get("/api/notifications")
async def proxy_notifications_root(request: Request):
    """Proxy GET /api/notifications (no trailing path)."""
    target_url = f"{NOTIFICATION_SERVICE_URL}/status/notifications"
    return await _proxy_request(request, target_url)


async def _proxy_request(request: Request, target_url: str):
    """Forward an HTTP request to a target URL."""
    token = request.headers.get("authorization", "")
    headers = {"Authorization": token, "Content-Type": "application/json"}
    params = dict(request.query_params)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if request.method == "GET":
                resp = await client.get(target_url, params=params, headers=headers)
            elif request.method == "POST":
                body = await request.body()
                resp = await client.post(target_url, content=body, params=params, headers=headers)
            elif request.method == "PUT":
                body = await request.body()
                resp = await client.put(target_url, content=body, params=params, headers=headers)
            elif request.method == "DELETE":
                resp = await client.delete(target_url, params=params, headers=headers)
            else:
                return JSONResponse({"error": "Method not allowed"}, status_code=405)

            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            return JSONResponse(data, status_code=resp.status_code)
    except Exception as e:
        logger.error("Proxy error to %s: %s", target_url, e)
        return JSONResponse({"error": f"Service unavailable: {str(e)}"}, status_code=502)


# --- WebSocket proxy ---

@app.websocket("/ws/notifications")
async def proxy_ws(websocket: WebSocket, token: str = ""):
    """Proxy WebSocket connection to the Notification Service."""
    await websocket.accept()

    ws_url = NOTIFICATION_SERVICE_URL.replace("http://", "ws://").replace("https://", "wss://")
    target = f"{ws_url}/status/notifications/ws/notifications?token={token}"

    try:
        async with httpx.AsyncClient() as client:
            import websockets
            async with websockets.connect(target) as upstream:
                import asyncio

                async def client_to_upstream():
                    try:
                        while True:
                            data = await websocket.receive_text()
                            await upstream.send(data)
                    except (WebSocketDisconnect, Exception):
                        pass

                async def upstream_to_client():
                    try:
                        async for message in upstream:
                            await websocket.send_text(message)
                    except Exception:
                        pass

                await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception as e:
        logger.warning("WS proxy error: %s", e)
        try:
            await websocket.close(code=1011, reason=str(e))
        except Exception:
            pass


# --- Static files ---

@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notification-ui"}


# Mount static files for any additional assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=UI_PORT)
