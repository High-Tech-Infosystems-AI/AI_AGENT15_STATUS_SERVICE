"""
Notification UI Server — lightweight FastAPI app on port 5009.

On startup, logs in to the Auth Service with configured credentials to get a real JWT.
All API calls use that server-held JWT — no login screen needed.

- GET  /                        → serves the dashboard SPA
- GET  /api/session              → returns current session info (token, user, role)
- GET  /api/users                → all users from Auth Service
- GET  /api/notifications*       → proxied through API Gateway
- POST /api/notifications*       → proxied through API Gateway
- PUT  /api/notifications*       → proxied through API Gateway
- WS   /ws/notifications         → proxied to Status Service
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification_ui")

# --- Config ---
API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://localhost:8050")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://localhost:8085")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8515")
UI_PORT = int(os.getenv("UI_PORT", "5009"))

# Credentials — must exist as a real user in the Auth Service DB
LOGIN_USERNAME = os.getenv("TEST_USERNAME", "supriyohti")
LOGIN_PASSWORD = os.getenv("TEST_PASSWORD", "891y29hdfabsf8128")

STATIC_DIR = Path(__file__).parent / "static"

# --- Server-held session ---
_session = {
    "token": None,
    "user_id": None,
    "role_name": None,
    "username": None,
}


async def _do_login() -> bool:
    """Login to Auth Service and store the JWT. Returns True on success."""
    login_urls = [
        f"{API_GATEWAY_URL}/auth/ats/login",
        f"{AUTH_SERVICE_URL}/ats/login",
    ]
    for url in login_urls:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD},
                )
                data = resp.json()
                if resp.status_code == 200 and data.get("access_token"):
                    _session["token"] = data["access_token"]
                    _session["user_id"] = data.get("user_id")
                    _session["role_name"] = data.get("role_name")
                    _session["username"] = LOGIN_USERNAME
                    logger.info(
                        "Logged in as %s (user_id=%s, role=%s) via %s",
                        LOGIN_USERNAME, _session["user_id"], _session["role_name"], url,
                    )
                    return True
                logger.warning("Login failed via %s: %s", url, data)
        except Exception as e:
            logger.warning("Login attempt failed via %s: %s", url, e)
            continue
    logger.error("Could not login with configured credentials")
    return False


def _get_token() -> Optional[str]:
    return _session.get("token")


def _auth_headers() -> dict:
    token = _get_token()
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# --- App ---

app = FastAPI(title="Notification Test UI", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_login():
    """Login on startup so the server has a valid JWT ready."""
    success = await _do_login()
    if not success:
        logger.error(
            "STARTUP LOGIN FAILED — UI will not be able to call APIs. "
            "Make sure TEST_USERNAME/TEST_PASSWORD are valid Auth Service credentials."
        )


# --- Session info (frontend fetches this on load instead of showing login) ---

@app.get("/api/session")
async def get_session():
    """Return current server session. Frontend uses this to check if logged in."""
    if not _session.get("token"):
        # Try to re-login
        await _do_login()
    return JSONResponse({
        "logged_in": _session.get("token") is not None,
        "username": _session.get("username"),
        "user_id": _session.get("user_id"),
        "role_name": _session.get("role_name"),
        "token": _session.get("token"),
    })


@app.post("/api/relogin")
async def relogin():
    """Force re-login (e.g. if token expired)."""
    success = await _do_login()
    if success:
        return JSONResponse({"success": True, "message": "Re-logged in successfully"})
    return JSONResponse({"success": False, "message": "Login failed"}, status_code=503)


# --- User list ---

@app.get("/api/users")
async def get_users():
    """Get all users from Auth Service using the server-held JWT."""
    token = _get_token()
    urls = [
        f"{API_GATEWAY_URL}/auth/ats/get_all_user",
        f"{AUTH_SERVICE_URL}/ats/get_all_user",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    url,
                    params={"token": token},
                    headers=_auth_headers(),
                )
                if resp.status_code == 200:
                    users = resp.json()
                    return JSONResponse({"users": users})
                # If 401, try re-login and retry once
                if resp.status_code == 401:
                    await _do_login()
                    resp2 = await client.get(url, params={"token": _get_token()}, headers=_auth_headers())
                    if resp2.status_code == 200:
                        return JSONResponse({"users": resp2.json()})
        except Exception as e:
            logger.warning("Failed to fetch users via %s: %s", url, e)
            continue
    return JSONResponse({"users": []})


# --- Notification API proxy (through API Gateway, using server-held JWT) ---

@app.get("/api/notifications")
async def proxy_notifications_root(request: Request):
    """Proxy GET /api/notifications (root)."""
    target_url = f"{API_GATEWAY_URL}/status/notifications/"
    return await _proxy_request(request, target_url)


@app.api_route("/api/notifications/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_notifications(request: Request, path: str):
    """Proxy all /api/notifications/* through the API Gateway."""
    target_url = f"{API_GATEWAY_URL}/status/notifications/{path}"
    return await _proxy_request(request, target_url)


async def _proxy_request(request: Request, target_url: str):
    """Forward request using the server-held JWT."""
    headers = _auth_headers()
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

            # If 401, try re-login and retry once
            if resp.status_code == 401:
                logger.info("Got 401, attempting re-login...")
                if await _do_login():
                    headers = _auth_headers()
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

            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            return JSONResponse(data, status_code=resp.status_code)
    except Exception as e:
        logger.error("Proxy error to %s: %s", target_url, e)
        return JSONResponse({"error": f"Service unavailable: {str(e)}"}, status_code=502)


# --- WebSocket proxy (direct to notification service) ---

@app.websocket("/ws/notifications")
async def proxy_ws(websocket: WebSocket, token: str = ""):
    """Proxy WebSocket using the server-held JWT (ignores client token param)."""
    await websocket.accept()

    # Always use the server-held token
    ws_token = _get_token() or token
    ws_url = NOTIFICATION_SERVICE_URL.replace("http://", "ws://").replace("https://", "wss://")
    target = f"{ws_url}/status/notifications/ws/notifications?token={ws_token}"

    try:
        import websockets
        async with websockets.connect(target) as upstream:

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
    return {"status": "ok", "service": "notification-ui", "logged_in": _session.get("token") is not None}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=UI_PORT)
