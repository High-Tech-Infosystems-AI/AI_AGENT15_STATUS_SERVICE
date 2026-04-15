"""
WebSocket Connection Manager for Notification Service.

Manages active WebSocket connections per user_id and fans out messages
from Redis Pub/Sub channels to connected clients.
"""

import asyncio
import json
import logging
from typing import Dict, Set

from fastapi import WebSocket

from app.notification_layer import redis_manager

logger = logging.getLogger("app_logger")


class NotificationWSManager:
    """
    Tracks active WebSocket connections keyed by user_id.
    One user can have multiple connections (multiple browser tabs).
    """

    def __init__(self):
        # user_id -> set of WebSocket connections
        self._connections: Dict[int, Set[WebSocket]] = {}
        self._subscriber_task: asyncio.Task = None

    async def connect(self, websocket: WebSocket, user_id: int) -> None:
        await websocket.accept()
        if user_id not in self._connections:
            self._connections[user_id] = set()
        self._connections[user_id].add(websocket)
        logger.info("WS connected: user_id=%s (total conns for user: %d)",
                     user_id, len(self._connections[user_id]))

    def disconnect(self, websocket: WebSocket, user_id: int) -> None:
        if user_id in self._connections:
            self._connections[user_id].discard(websocket)
            if not self._connections[user_id]:
                del self._connections[user_id]
        logger.info("WS disconnected: user_id=%s", user_id)

    @property
    def connected_user_ids(self) -> set:
        return set(self._connections.keys())

    async def send_to_user(self, user_id: int, data: dict) -> None:
        """Send a message to all connections of a specific user."""
        sockets = self._connections.get(user_id, set()).copy()
        for ws in sockets:
            try:
                await ws.send_json(data)
            except Exception:
                self._connections.get(user_id, set()).discard(ws)

    async def send_to_users(self, user_ids: list, data: dict) -> None:
        """Send a message to multiple users."""
        for uid in user_ids:
            await self.send_to_user(uid, data)

    async def broadcast(self, data: dict) -> None:
        """Send to ALL connected users."""
        for user_id in list(self._connections.keys()):
            await self.send_to_user(user_id, data)

    @staticmethod
    def _msg_type_for(payload: dict) -> str:
        """Map a notification payload's delivery_mode to its WS message type."""
        if not isinstance(payload, dict):
            return "notification"
        mode = payload.get("delivery_mode")
        if mode == "log":
            return "log"
        if mode == "banner":
            return "banner"
        return "notification"

    async def start_redis_subscriber(self) -> None:
        """
        Background task that subscribes to Redis Pub/Sub and delivers
        messages to connected WebSocket clients.
        """
        if self._subscriber_task and not self._subscriber_task.done():
            return
        self._subscriber_task = asyncio.create_task(self._redis_listener())

    async def _redis_listener(self) -> None:
        """Long-running coroutine: listens to Redis and dispatches to WebSockets."""
        while True:
            try:
                r = redis_manager.get_pubsub_redis()
                pubsub = r.pubsub()

                # Subscribe to broadcast + banner channels
                pubsub.subscribe("notif:broadcast", "notif:banner")

                # We use pattern subscribe for per-user channels
                pubsub.psubscribe("notif:user:*")

                logger.info("Redis Pub/Sub listener started for notifications")

                while True:
                    message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is None:
                        await asyncio.sleep(0.1)
                        continue

                    channel = message.get("channel", "")
                    data_str = message.get("data", "")
                    if not isinstance(data_str, str):
                        continue

                    try:
                        payload = json.loads(data_str)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    # Route based on channel.
                    # For broadcast + per-user channels we inspect the payload's
                    # delivery_mode so logs go out as type="log" (not "notification").
                    if channel == "notif:broadcast":
                        msg_type = self._msg_type_for(payload)
                        await self.broadcast({"type": msg_type, "data": payload})

                    elif channel == "notif:banner":
                        # Banner events: route to specific recipients only.
                        # Payload format: {"type": "banner", "action": "create|expire", "data": {..., "recipient_ids": [...]}}
                        data_field = payload.get("data") if isinstance(payload, dict) else None
                        recipient_ids = None
                        if isinstance(data_field, dict):
                            recipient_ids = data_field.get("recipient_ids")
                        if recipient_ids:
                            # Strip recipient_ids from payload before sending to client (internal routing detail)
                            forward_payload = dict(payload)
                            forward_data = dict(data_field)
                            forward_data.pop("recipient_ids", None)
                            forward_payload["data"] = forward_data
                            for uid in recipient_ids:
                                if uid in self._connections:
                                    await self.send_to_user(uid, forward_payload)
                        else:
                            # Backward-compat fallback: no recipient_ids → broadcast (e.g. system banners)
                            await self.broadcast(payload)

                    elif channel.startswith("notif:user:"):
                        try:
                            user_id = int(channel.split(":")[-1])
                            # Distinguish meta messages from notifications
                            if isinstance(payload, dict) and payload.get("_meta") == "unread_count":
                                data_out = {"count": payload.get("count", 0)}
                                # Forward per-mode counts if present
                                for k in ("push", "banner", "log", "total"):
                                    if k in payload:
                                        data_out[k] = payload[k]
                                await self.send_to_user(user_id, {
                                    "type": "unread_count",
                                    "data": data_out,
                                })
                            elif isinstance(payload, dict) and payload.get("_meta") == "banners_snapshot":
                                # Full per-user active-banner snapshot (sent on create/expire)
                                await self.send_to_user(user_id, {
                                    "type": "banners",
                                    "action": "snapshot",
                                    "data": payload.get("data", []),
                                })
                            else:
                                msg_type = self._msg_type_for(payload)
                                await self.send_to_user(user_id, {"type": msg_type, "data": payload})
                        except (ValueError, IndexError):
                            pass

            except Exception as e:
                logger.error("Redis Pub/Sub listener error: %s. Reconnecting in 3s...", e)
                await asyncio.sleep(3)


# Global singleton
ws_manager = NotificationWSManager()
