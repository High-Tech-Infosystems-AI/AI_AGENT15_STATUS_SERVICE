"""ChatWSManager - mirrors NotificationWSManager pattern, separate channels."""
import asyncio
import json
import logging
from typing import Dict, Set

from fastapi import WebSocket

from app.notification_layer import redis_manager

logger = logging.getLogger("app_logger")


class ChatWSManager:
    def __init__(self):
        self._connections: Dict[int, Set[WebSocket]] = {}
        self._subscriber_task = None

    async def connect(self, ws: WebSocket, user_id: int) -> None:
        await ws.accept()
        self._connections.setdefault(user_id, set()).add(ws)
        logger.info("chat WS connect user=%s total=%d",
                    user_id, len(self._connections[user_id]))

    def disconnect(self, ws: WebSocket, user_id: int) -> None:
        if user_id in self._connections:
            self._connections[user_id].discard(ws)
            if not self._connections[user_id]:
                del self._connections[user_id]

    def is_online(self, user_id: int) -> bool:
        return user_id in self._connections and bool(self._connections[user_id])

    @property
    def online_user_ids(self) -> set:
        return set(self._connections.keys())

    async def send_to_user(self, user_id: int, data: dict) -> None:
        for ws in list(self._connections.get(user_id, set())):
            try:
                await ws.send_json(data)
            except Exception:
                self._connections.get(user_id, set()).discard(ws)

    async def start_redis_subscriber(self) -> None:
        if self._subscriber_task and not self._subscriber_task.done():
            return
        self._subscriber_task = asyncio.create_task(self._listener())

    async def _listener(self) -> None:
        loop = asyncio.get_event_loop()

        def _poll(p):
            return p.get_message(ignore_subscribe_messages=True, timeout=0.5)

        while True:
            try:
                r = redis_manager.get_pubsub_redis()
                pubsub = r.pubsub()
                pubsub.psubscribe("chat:user:*")
                logger.info("Chat Redis subscriber started")
                while True:
                    msg = await loop.run_in_executor(None, _poll, pubsub)
                    if msg is None:
                        continue
                    channel = msg.get("channel", "")
                    data_str = msg.get("data", "")
                    if not isinstance(data_str, str) or not channel.startswith("chat:user:"):
                        continue
                    try:
                        payload = json.loads(data_str)
                        uid = int(channel.split(":")[-1])
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if uid in self._connections:
                        await self.send_to_user(uid, payload)
            except Exception as e:
                logger.error("ChatWS subscriber error: %s", e)
                await asyncio.sleep(3)


ws_manager = ChatWSManager()
