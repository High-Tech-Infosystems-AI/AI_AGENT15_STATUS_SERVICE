"""Real-time mail delivery: per-user WebSocket manager + Redis pub/sub fan-out.

The IMAP IDLE worker runs in a SEPARATE process from the mail API, so it can't
touch the live WebSocket connections directly. Instead it PUBLISHES a mail event
to Redis (channel ``mail:user:<id>``); this manager — running inside the mail API
process — subscribes to that pattern and pushes the event to the user's
browser(s). Mirrors app/notification_layer/ws_manager.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Set

from fastapi import WebSocket

from app.notification_layer import redis_manager

logger = logging.getLogger("app_logger")

MAIL_CHANNEL_PREFIX = "mail:user:"


def publish_mail_event(user_id: int, event: dict) -> None:
    """Publish a mail event to a user's channel. Safe to call from any process
    (e.g. the IDLE worker); the mail API's subscriber delivers it to the browser."""
    if not user_id:
        return
    try:
        redis_manager.get_notification_redis().publish(
            f"{MAIL_CHANNEL_PREFIX}{user_id}", json.dumps(event, default=str))
    except Exception as exc:
        logger.warning("mail realtime publish failed for user %s: %s", user_id, exc)


class MailWSManager:
    """Tracks live WebSocket connections keyed by user_id (multiple tabs allowed)."""

    def __init__(self):
        self._connections: Dict[int, Set[WebSocket]] = {}
        self._subscriber_task: asyncio.Task | None = None

    async def connect(self, websocket: WebSocket, user_id: int) -> None:
        await websocket.accept()
        self._connections.setdefault(user_id, set()).add(websocket)
        logger.info("mail WS connected: user=%s (conns=%d)",
                    user_id, len(self._connections[user_id]))

    def disconnect(self, websocket: WebSocket, user_id: int) -> None:
        conns = self._connections.get(user_id)
        if conns:
            conns.discard(websocket)
            if not conns:
                self._connections.pop(user_id, None)
        logger.info("mail WS disconnected: user=%s", user_id)

    async def send_to_user(self, user_id: int, data: dict) -> None:
        for ws in self._connections.get(user_id, set()).copy():
            try:
                await ws.send_json(data)
            except Exception:
                self._connections.get(user_id, set()).discard(ws)

    async def start_redis_subscriber(self) -> None:
        if self._subscriber_task and not self._subscriber_task.done():
            return
        self._subscriber_task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        loop = asyncio.get_event_loop()

        def _poll(pubsub_client):
            # Blocking — run in a thread so the API event loop stays responsive.
            return pubsub_client.get_message(ignore_subscribe_messages=True, timeout=0.5)

        while True:
            try:
                pubsub = redis_manager.get_pubsub_redis().pubsub()
                pubsub.psubscribe(f"{MAIL_CHANNEL_PREFIX}*")
                logger.info("mail Redis pub/sub listener started")
                while True:
                    message = await loop.run_in_executor(None, _poll, pubsub)
                    if not message:
                        continue
                    channel = message.get("channel", "")
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    data_str = message.get("data", "")
                    if isinstance(data_str, bytes):
                        data_str = data_str.decode()
                    if not isinstance(data_str, str):
                        continue
                    try:
                        payload = json.loads(data_str)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if channel.startswith(MAIL_CHANNEL_PREFIX):
                        try:
                            uid = int(channel.rsplit(":", 1)[-1])
                        except ValueError:
                            continue
                        await self.send_to_user(uid, payload)
            except Exception as exc:
                logger.error("mail Redis listener error: %s. Reconnecting in 3s...", exc)
                await asyncio.sleep(3)


ws_manager = MailWSManager()
