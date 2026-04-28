"""
Dev tester server — serves index.html and proxies /auth/* and /chat/*
(including the /chat/ws WebSocket) to the dev gateway. Same-origin
deployment, so the browser never has a CORS problem.

Run from this directory:

    uv run --no-project python serve.py
or
    python serve.py

Then open http://localhost:5173
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

# -- config -----------------------------------------------------------------

UPSTREAM_HTTP = "https://devai.api.htinfosystems.com"
UPSTREAM_WS   = "wss://devai.api.htinfosystems.com"
LISTEN_PORT   = 5173

# Forwarded paths
PROXY_PREFIXES = ("/auth/", "/chat/")

# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("chat-tester-proxy")

DIR = Path(__file__).parent

# httpx client reused across requests (HTTP keepalive + connection pooling)
http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=False)
    log.info("Tester ready at http://127.0.0.1:%s", LISTEN_PORT)
    log.info("Proxying %s, %s -> %s", *PROXY_PREFIXES, UPSTREAM_HTTP)
    yield
    await http_client.aclose()


app = FastAPI(title="Chat Tester Proxy", lifespan=lifespan)


@app.get("/")
@app.get("/index.html")
def index():
    """Serve the React tester."""
    return FileResponse(DIR / "index.html")


# ---- HTTP proxy ----

@app.api_route("/auth/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/chat/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def http_proxy(request: Request, path: str):
    upstream_url = f"{UPSTREAM_HTTP}{request.url.path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Pass through everything except hop-by-hop and host-specific headers
    drop = {"host", "content-length", "connection", "accept-encoding"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}

    body = await request.body()
    log.info("%s %s -> %s", request.method, request.url.path, upstream_url)

    try:
        upstream = await http_client.request(
            request.method,
            upstream_url,
            headers=headers,
            content=body or None,
        )
    except httpx.RequestError as exc:
        log.error("upstream error: %s", exc)
        return Response(
            content=f'{{"error": "upstream unreachable: {exc}"}}',
            status_code=502,
            media_type="application/json",
        )

    # Strip hop-by-hop response headers
    drop_resp = {"content-encoding", "transfer-encoding", "content-length",
                 "connection", "keep-alive"}
    response_headers = {k: v for k, v in upstream.headers.items()
                        if k.lower() not in drop_resp}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ---- WebSocket proxy for /chat/ws ----

@app.websocket("/chat/ws")
async def ws_proxy(client: WebSocket):
    """Bidirectional pipe between the browser and the upstream chat WS."""
    token = client.query_params.get("token", "")
    upstream_url = f"{UPSTREAM_WS}/chat/ws?token={token}"
    await client.accept()
    log.info("WS open -> %s", upstream_url[:80] + "...")

    try:
        async with websockets.connect(upstream_url, max_size=10 * 1024 * 1024) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        msg = await client.receive_text()
                        await upstream.send(msg)
                except WebSocketDisconnect:
                    log.info("WS client disconnected")
                except Exception as e:
                    log.warning("WS c2u error: %s", e)

            async def upstream_to_client():
                try:
                    async for msg in upstream:
                        # websockets returns str for text frames, bytes for binary
                        if isinstance(msg, bytes):
                            await client.send_bytes(msg)
                        else:
                            await client.send_text(msg)
                except websockets.ConnectionClosed:
                    log.info("WS upstream closed")
                except Exception as e:
                    log.warning("WS u2c error: %s", e)

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except websockets.InvalidStatusCode as exc:
        # Upstream rejected the handshake — usually 401 (bad/missing token)
        await client.close(code=4001, reason=f"upstream rejected: {exc.status_code}")
    except Exception as exc:
        log.error("WS proxy error: %s", exc)
        try:
            await client.close(code=1011, reason=str(exc))
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=LISTEN_PORT, log_level="info")
