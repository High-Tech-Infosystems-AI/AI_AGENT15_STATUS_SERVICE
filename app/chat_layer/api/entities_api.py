"""Picker + resolver endpoints used by the chat composer's `+` menu and
inline @-autocomplete, and by the message renderer to fetch a fresh card.

Routes (all under /chat):
  GET  /entities/search?type=<t>&q=<q>&limit=12&conversation_id=<id?>
  GET  /entities/{type}/{id}
  POST /entities/resolve              {"refs": [{"type":..., "id":...}, ...]}
  GET  /entities/reports/catalog

Scoping rules for `search`:
  - No conversation_id (or team / general / inbox-level picker):
      Admin sees everything; non-admin is scoped to their own assignments.
  - DM conversation:
      Only Admin / SuperAdmin senders may use the picker.
      If the DM peer is also an admin, no scoping applies.
      If the DM peer is a regular user, results are scoped to entities
      assigned to that peer (so the admin only references things the
      recipient can actually act on).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.chat_layer import entity_resolver, store
from app.chat_layer.auth import current_user
from app.chat_layer.models import ChatConversation
from app.database_Layer.db_config import SessionLocal

router = APIRouter()


def _err(code: str, msg: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error_code": code, "message": msg})


class _Ref(BaseModel):
    type: str
    id: str | int


class ResolveRequest(BaseModel):
    refs: List[_Ref] = Field(..., max_length=50)


def _scope_for_search(db, *, caller_id: int, caller_role: Optional[str],
                      conversation_id: Optional[int]) -> tuple[Optional[int], Optional[JSONResponse]]:
    """Resolve the `scope_user_id` argument the resolver should use, given
    the conversation context. Returns (scope_user_id, error_response).
    If the second element is non-None, the API should return it directly.
    """
    caller_is_admin = entity_resolver.is_admin_role(caller_role)

    # No conversation context → caller-level scoping (admins unscoped).
    if not conversation_id:
        return (None if caller_is_admin else caller_id, None)

    conv = db.get(ChatConversation, conversation_id)
    if not conv or conv.deleted_at is not None:
        return (None, _err("CHAT_NOT_FOUND", "Conversation not found", 404))
    if not store.is_member(db, conversation_id, caller_id):
        return (None, _err("CHAT_NOT_MEMBER", "Not a conversation member", 403))

    # Team / general → caller-level scoping (admins unscoped).
    if conv.type != "dm":
        return (None if caller_is_admin else caller_id, None)

    # DM: only admins are allowed to tag entities at all.
    if not caller_is_admin:
        return (None, _err(
            "CHAT_ENTITY_DM_FORBIDDEN",
            "Entity references in direct messages are restricted to admins.",
            403,
        ))

    # Admin in a DM. Look at the peer to decide whether to scope.
    members = store.member_user_ids(db, conversation_id)
    peer_id = next((m for m in members if m != caller_id), None)
    if peer_id is None:
        return (None, _err("CHAT_NOT_MEMBER", "DM peer missing", 404))
    peer_role = entity_resolver.get_user_role_name(db, peer_id)
    if entity_resolver.is_admin_role(peer_role):
        # Admin ↔ admin DM: full picker, no scoping.
        return (None, None)
    # Admin ↔ user DM: scope to entities visible to the recipient user.
    return (peer_id, None)


@router.get("/entities/search")
def search_entities(type: str, q: str = "", limit: int = 12,
                    conversation_id: Optional[int] = None,
                    user: dict = Depends(current_user)):
    if type not in entity_resolver.ENTITY_TYPES:
        return _err("ENTITY_BAD_TYPE",
                    f"Unknown entity type. Allowed: {entity_resolver.ENTITY_TYPES}", 400)
    db = SessionLocal()
    try:
        scope_user_id, err = _scope_for_search(
            db,
            caller_id=user.get("user_id"),
            caller_role=user.get("role_name"),
            conversation_id=conversation_id,
        )
        if err:
            return err
        cards = entity_resolver.search(
            db, type_=type, q=q or "", limit=max(1, min(limit, 50)),
            scope_user_id=scope_user_id,
        )
        return {"items": cards}
    finally:
        db.close()


@router.get("/entities/reports/catalog")
def reports_catalog(_user: dict = Depends(current_user)):
    """List of shareable reports/graphs the user can drop into a chat."""
    return {"items": entity_resolver.REPORTS_CATALOG}


@router.get("/entities/{type}/{entity_id}")
def get_entity(type: str, entity_id: str,
               _user: dict = Depends(current_user)):
    if type not in entity_resolver.ENTITY_TYPES:
        return _err("ENTITY_BAD_TYPE", "Unknown entity type", 400)
    # Most entity types have integer PKs, but candidates use a string
    # `candidate_id` and reports use slug ids. Coerce to int when possible
    # and fall through to string otherwise.
    rid: str | int = entity_id
    if type not in ("report", "candidate"):
        try:
            rid = int(entity_id)
        except ValueError:
            return _err("ENTITY_BAD_ID", "Numeric id required for this type", 400)
    db = SessionLocal()
    try:
        cards = entity_resolver.resolve(db, [{"type": type, "id": rid}])
        if not cards or not cards[0]:
            return _err("ENTITY_NOT_FOUND", "Entity not found", 404)
        return cards[0]
    finally:
        db.close()


@router.post("/entities/resolve")
def resolve_entities(req: ResolveRequest, _user: dict = Depends(current_user)):
    """Bulk resolve — used by the message renderer when a message arrives
    with `references[]` and the FE needs full cards in one round-trip."""
    db = SessionLocal()
    try:
        cards: List[Optional[dict]] = entity_resolver.resolve(
            db, [r.model_dump() for r in req.refs],
        )
        return {"items": cards}
    finally:
        db.close()
