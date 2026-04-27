"""Presence endpoint."""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.chat_layer.auth import current_user
from app.chat_layer.models import ChatUserPresence
from app.chat_layer.schemas import PresenceOut
from app.database_Layer.db_config import SessionLocal

router = APIRouter()


def _fetch_presence(db, user_id: int):
    return db.get(ChatUserPresence, user_id)


@router.get("/users/{user_id}/presence", response_model=PresenceOut)
def get_presence(user_id: int, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        row = _fetch_presence(db, user_id)
        if row is None:
            return JSONResponse(content={
                "user_id": user_id, "status": "offline", "last_seen_at": None,
            })
        return PresenceOut.model_validate(row).model_dump(mode="json")
    finally:
        db.close()
