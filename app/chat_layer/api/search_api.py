"""Search endpoint - scoped to caller's conversations."""
from types import SimpleNamespace
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.chat_layer.api.messages_api import _fetch_attachment, _to_message_out
from app.chat_layer.auth import current_user
from app.chat_layer.schemas import PaginatedMessages
from app.database_Layer.db_config import SessionLocal

router = APIRouter()


def _has_fulltext(db) -> bool:
    """Detect whether ft_body exists. Falls back to LIKE if not."""
    try:
        row = db.execute(text(
            "SHOW INDEX FROM chat_messages WHERE Key_name = 'ft_body'"
        )).first()
        return row is not None
    except Exception:
        return False


def _search(db, *, user_id: int, q: str, conversation_id: Optional[int],
            limit: int) -> Tuple[List, bool, Optional[str]]:
    if _has_fulltext(db):
        sql = ("SELECT m.* FROM chat_messages m "
               "JOIN chat_conversation_members cm "
               "  ON cm.conversation_id = m.conversation_id "
               "WHERE cm.user_id = :uid AND m.deleted_at IS NULL "
               "  AND MATCH(m.body) AGAINST (:q IN BOOLEAN MODE) ")
    else:
        sql = ("SELECT m.* FROM chat_messages m "
               "JOIN chat_conversation_members cm "
               "  ON cm.conversation_id = m.conversation_id "
               "WHERE cm.user_id = :uid AND m.deleted_at IS NULL "
               "  AND m.body LIKE :like_q ")
    params = {"uid": user_id, "q": q, "like_q": f"%{q}%"}
    if conversation_id:
        sql += "AND m.conversation_id = :cid "
        params["cid"] = conversation_id
    sql += "ORDER BY m.created_at DESC, m.id DESC LIMIT :lim"
    params["lim"] = limit + 1
    rows = list(db.execute(text(sql), params).all())
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    items = [SimpleNamespace(**dict(r._mapping)) for r in rows]
    return items, has_more, None


@router.get("/search", response_model=PaginatedMessages)
def search(q: str, conversation_id: Optional[int] = None, limit: int = 50,
           user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows, has_more, _ = _search(
            db, user_id=user["user_id"], q=q,
            conversation_id=conversation_id, limit=min(max(limit, 1), 100),
        )
        out = []
        for m in rows:
            att = _fetch_attachment(db, getattr(m, "attachment_id", None)) \
                  if getattr(m, "attachment_id", None) else None
            out.append(_to_message_out(m, attachment=att))
        return PaginatedMessages(items=out, has_more=has_more,
                                 next_cursor=None).model_dump(mode="json")
    finally:
        db.close()
