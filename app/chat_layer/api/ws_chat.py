"""WS /chat/ws?token=... - chat-specific events stream."""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.chat_layer import presence, redis_chat, store
from app.chat_layer.auth import _validate_token
from app.chat_layer.models import ChatConversation, ChatMessage
from app.chat_layer.ws_manager import ws_manager
from app.database_Layer.db_config import SessionLocal

logger = logging.getLogger("app_logger")
router = APIRouter()


def _on_connect(user_id) -> list:
    """Blocking part of a connect (Redis + DB): presence online, fan-out, snapshot."""
    redis_chat.set_presence_online(user_id)
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        store.upsert_presence(db, user_id, "online", last_seen_at=now)
        # Tell co-members "I just came online"
        presence.fan_out_presence(db=db, user_id=user_id, status="online",
                                  last_seen_at=now)
        # Tell THIS connection who is currently online — covers the case
        # where co-members connected before us (we'd otherwise see them as
        # offline forever, since Pub/Sub events from earlier are lost).
        return presence.get_presence_snapshot(db, user_id)
    finally:
        db.close()


def _on_typing(user_id, conv_id, state) -> None:
    if state == "start":
        redis_chat.set_typing(conv_id, user_id)
    else:
        redis_chat.clear_typing(conv_id, user_id)
    db = SessionLocal()
    try:
        for uid in store.member_user_ids(db, conv_id):
            if uid != user_id:
                redis_chat.publish_typing(uid, conv_id, user_id, state)
    finally:
        db.close()


def _on_mark_read(user_id, mids) -> None:
    db = SessionLocal()
    try:
        affected_convs: set = set()
        for mid in mids:
            m = db.get(ChatMessage, mid)
            if not m:
                continue
            conv = db.get(ChatConversation, m.conversation_id)
            if not conv:
                continue
            store.mark_read(db, message_id=mid, user_id=user_id)
            store.update_last_read(db, conversation_id=conv.id,
                                   user_id=user_id, message_id=mid)
            now = datetime.utcnow().isoformat()
            if conv.type == "dm":
                redis_chat.publish_message_read(
                    user_id=m.sender_id, message_id=mid,
                    reader_user_id=user_id, read_at=now,
                )
            else:
                rc = store.read_count(db, mid)
                for uid in store.member_user_ids(db, conv.id):
                    redis_chat.publish_message_read_count(
                        user_id=uid, message_id=mid,
                        conversation_id=conv.id, read_count=rc,
                    )
            affected_convs.add(conv.id)
        for cid in affected_convs:
            unread = store.unread_count_for_user(db, cid, user_id)
            redis_chat.publish_unread_update(
                user_id=user_id, conversation_id=cid,
                unread_count=unread,
            )
    finally:
        db.close()


def _on_disconnect(user_id) -> None:
    redis_chat.clear_presence(user_id)
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        store.upsert_presence(db, user_id, "offline", last_seen_at=now)
        presence.fan_out_presence(db=db, user_id=user_id, status="offline",
                                  last_seen_at=now)
    finally:
        db.close()


# Every Redis, DB and auth call below is blocking, so it runs in the threadpool:
# done on the event loop, each connect / disconnect froze the whole chat app (the
# database is on another node), the 5 s Consul health check timed out, and the
# gateway answered 503 for every /chat request while the service was "critical".
@router.websocket("/ws")
async def ws_chat(websocket: WebSocket, token: str = Query(...)):
    info = await run_in_threadpool(_validate_token, token)
    if not info:
        await websocket.close(code=4001, reason="Invalid token")
        return
    user_id = info["user_id"]
    await ws_manager.connect(websocket, user_id)
    try:
        snapshot = await run_in_threadpool(_on_connect, user_id)
    except Exception as exc:
        logger.warning("chat ws connect bookkeeping failed for user=%s: %s", user_id, exc)
        snapshot = []

    for entry in snapshot:
        try:
            await websocket.send_json({"type": "presence.update", "data": entry})
        except Exception as exc:
            logger.warning("presence snapshot send failed: %s", exc)
            break

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            action = msg.get("action")
            if action == "ping":
                await run_in_threadpool(redis_chat.refresh_presence, user_id)
                await websocket.send_json({"type": "pong"})
            elif action == "typing":
                conv_id = msg.get("conversation_id")
                if conv_id:
                    await run_in_threadpool(_on_typing, user_id, conv_id,
                                            msg.get("state", "start"))
            elif action == "mark_read":
                # Accept either `message_id` (single, legacy) or
                # `message_ids` (bulk array). Bulk calls coalesce the
                # `unread.update` event so the badge clears once per conv.
                mids = []
                if isinstance(msg.get("message_ids"), list):
                    mids = [int(x) for x in msg["message_ids"] if x]
                elif msg.get("message_id"):
                    mids = [int(msg["message_id"])]
                if not mids:
                    continue
                await run_in_threadpool(_on_mark_read, user_id, mids)
    except WebSocketDisconnect:
        logger.info("ws_chat disconnect user=%s", user_id)
    finally:
        ws_manager.disconnect(websocket, user_id)
        try:
            await run_in_threadpool(_on_disconnect, user_id)
        except Exception as exc:
            logger.warning("chat ws disconnect bookkeeping failed for user=%s: %s", user_id, exc)
