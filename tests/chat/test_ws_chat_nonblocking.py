"""The chat WebSocket must not block the event loop.

Its token check, Redis and DB calls are blocking. Run on the loop, every connect /
disconnect froze the chat app long enough for the 5 s Consul health check to fail,
and the gateway answered 503 for all /chat requests.
"""
import asyncio
import os
import tempfile
import time

# Dummy settings so `app.core` imports without a .env (never real credentials).
for _k, _v in {
    "APP_ENV": "dev", "DB_HOST": "127.0.0.1", "DB_PORT": "3306", "DB_NAME": "test",
    "DB_USER": "test", "DB_PASSWORD": "test", "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "6399",
    "REDIS_DB": "0", "REDIS_PASSWORD": "", "CONSUL_ENABLED": "false",
    "AUTH_SERVICE_URL": "http://127.0.0.1:9/verify", "ACCESS_TOKEN_EXPIRE_HOURS": "1",
    "JWT_SECRET_KEY": "test", "JWT_ALGORITHM": "HS256", "BASE_URL": "http://127.0.0.1:9",
}.items():
    os.environ.setdefault(_k, _v)
os.environ.setdefault("LOG_FILE_PATH", os.path.join(tempfile.gettempdir(), "status_service_test_logs"))

from fastapi import WebSocketDisconnect  # noqa: E402

from app.chat_layer.api import ws_chat as W  # noqa: E402


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_text(self):
        raise WebSocketDisconnect()

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)


def test_slow_connect_and_disconnect_do_not_block_the_loop(monkeypatch):
    def slow(*a, **k):
        time.sleep(0.4)

    monkeypatch.setattr(W, "_validate_token", lambda t: (time.sleep(0.4), {"user_id": 7})[1])
    monkeypatch.setattr(W, "_on_connect", lambda uid: (slow(), [{"user_id": 8, "status": "online"}])[1])
    monkeypatch.setattr(W, "_on_disconnect", slow)

    async def fake_connect(ws, uid):
        return None
    monkeypatch.setattr(W.ws_manager, "connect", fake_connect)
    monkeypatch.setattr(W.ws_manager, "disconnect", lambda ws, uid: None)

    async def main():
        ticks = 0
        done = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.02)

        t = asyncio.create_task(ticker())
        ws = _FakeWS()
        await W.ws_chat(ws, token="x")
        done.set()
        await t
        return ticks, ws

    ticks, ws = asyncio.run(main())
    # 1.2 s of blocking work ran in threads: the loop kept ticking (~60 ticks).
    assert ticks >= 30
    assert ws.sent == [{"type": "presence.update", "data": {"user_id": 8, "status": "online"}}]


def test_invalid_token_closes_without_touching_db(monkeypatch):
    monkeypatch.setattr(W, "_validate_token", lambda t: None)
    called = []
    monkeypatch.setattr(W, "_on_connect", lambda uid: called.append(uid) or [])
    ws = _FakeWS()
    asyncio.run(W.ws_chat(ws, token="bad"))
    assert ws.closed == (4001, "Invalid token") and called == []
