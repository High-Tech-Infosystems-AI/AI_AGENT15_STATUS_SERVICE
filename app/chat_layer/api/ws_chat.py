"""WS /chat/ws?token=... - chat-specific events stream."""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.chat_layer import presence, redis_chat, store
from app.chat_layer.auth import _validate_token
from app.chat_layer.models import ChatConversation, ChatMessage
from app.chat_layer.ws_manager import ws_manager
from app.database_Layer.db_config import SessionLocal

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.websocket("/ws")
async def ws_chat(websocket: WebSocket, token: str = Query(...)):
    info = _validate_token(token)
    if not info:
        await websocket.close(code=4001, reason="Invalid token")
        return
    user_id = info["user_id"]
    await ws_manager.connect(websocket, user_id)
    redis_chat.set_presence_online(user_id)

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        store.upsert_presence(db, user_id, "online", last_seen_at=now)
        presence.fan_out_presence(db=db, user_id=user_id, status="online",
                                  last_seen_at=now)
    finally:
        db.close()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            action = msg.get("action")
            if action == "ping":
                redis_chat.refresh_presence(user_id)
                await websocket.send_json({"type": "pong"})
            elif action == "typing":
                conv_id = msg.get("conversation_id")
                state = msg.get("state", "start")
                if conv_id:
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
            elif action == "mark_read":
                mid = msg.get("message_id")
                if mid:
                    db = SessionLocal()
                    try:
                        store.mark_read(db, message_id=mid, user_id=user_id)
                        m = db.get(ChatMessage, mid)
                        if m:
                            conv = db.get(ChatConversation, m.conversation_id)
                            store.update_last_read(db, conversation_id=m.conversation_id,
                                                   user_id=user_id, message_id=m.id)
                            if conv and conv.type == "dm":
                                redis_chat.publish_message_read(
                                    user_id=m.sender_id, message_id=m.id,
                                    reader_user_id=user_id,
                                    read_at=datetime.utcnow().isoformat(),
                                )
                            elif conv:
                                rc = store.read_count(db, m.id)
                                for uid in store.member_user_ids(db, m.conversation_id):
                                    redis_chat.publish_message_read_count(
                                        user_id=uid, message_id=m.id,
                                        conversation_id=m.conversation_id,
                                        read_count=rc,
                                    )
                            # Cross-tab badge clear
                            unread = store.unread_count_for_user(
                                db, m.conversation_id, user_id,
                            )
                            redis_chat.publish_unread_update(
                                user_id=user_id, conversation_id=m.conversation_id,
                                unread_count=unread,
                            )
                    finally:
                        db.close()
    except WebSocketDisconnect:
        logger.info("ws_chat disconnect user=%s", user_id)
    finally:
        ws_manager.disconnect(websocket, user_id)
        redis_chat.clear_presence(user_id)
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            store.upsert_presence(db, user_id, "offline", last_seen_at=now)
            presence.fan_out_presence(db=db, user_id=user_id, status="offline",
                                      last_seen_at=now)
        finally:
            db.close()
