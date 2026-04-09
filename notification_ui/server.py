"""
Notification UI Server — simple proxy to the Status Service /test endpoints.
No JWT, no auth. All test endpoints on the Status Service are open.

Port 5009 (UI) → Port 8515 (Status Service /test/*)
"""

import os
import asyncio
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification_ui")

STATUS_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8515")
UI_PORT = int(os.getenv("UI_PORT", "5009"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Notification Test UI", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# --- Proxy all /api/* to Status Service /test/* ---

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_api(request: Request, path: str):
    """Proxy /api/* → Status Service /test/*"""
    target = f"{STATUS_SERVICE_URL}/test/{path}"
    params = dict(request.query_params)
    headers = {"Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
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


# --- WebSocket proxy → Status Service /test/ws/notifications ---

@app.websocket("/ws/notifications")
async def proxy_ws(websocket: WebSocket, user_id: int = 1):
    """Proxy WS to the Status Service test WebSocket endpoint."""
    await websocket.accept()

    ws_url = STATUS_SERVICE_URL.replace("http://", "ws://").replace("https://", "wss://")
    target = f"{ws_url}/test/ws/notifications?user_id={user_id}"

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
