"""Picker + resolver endpoints used by the chat composer's `+` menu and
inline @-autocomplete, and by the message renderer to fetch a fresh card.

Routes (all under /chat):
  GET  /entities/search?type=<t>&q=<q>&limit=12
  GET  /entities/{type}/{id}
  POST /entities/resolve              {"refs": [{"type":..., "id":...}, ...]}
  GET  /entities/reports/catalog
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.chat_layer import entity_resolver
from app.chat_layer.auth import current_user
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


@router.get("/entities/search")
def search_entities(type: str, q: str = "", limit: int = 12,
                    _user: dict = Depends(current_user)):
    if type not in entity_resolver.ENTITY_TYPES:
        return _err("ENTITY_BAD_TYPE",
                    f"Unknown entity type. Allowed: {entity_resolver.ENTITY_TYPES}", 400)
    db = SessionLocal()
    try:
        cards = entity_resolver.search(
            db, type_=type, q=q or "", limit=max(1, min(limit, 50)),
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
    rid: str | int = entity_id
    if type != "report":
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
