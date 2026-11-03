"""Shared helpers for the WhatsApp / clawback-notification routers."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.dependencies.tenant_auth import (
    AuthCtx, assert_row_in_scope, resolve_read_org, resolve_write_org,
)

MAX_LIMIT = 100


def read_org(organization_id: Optional[int], ctx: AuthCtx) -> int:
    """Org for a single-org read. Super admins must pick one."""
    org = resolve_read_org(organization_id, ctx)
    if org is None:
        raise HTTPException(400, "organization_id is required when acting as Super Admin")
    return org


def write_org(query_org: Optional[int], body_org: Optional[int], ctx: AuthCtx) -> int:
    """Super admins pass organization_id as a query param or in the body."""
    return resolve_write_org(query_org if query_org is not None else body_org, ctx)


def unprocessable(errors: List[Dict[str, str]]) -> None:
    if errors:
        raise HTTPException(status_code=422, detail=errors)


def page_params(page: int, limit: int) -> Tuple[int, int]:
    page = max(1, int(page or 1))
    limit = max(1, min(MAX_LIMIT, int(limit or 25)))
    return page, limit


def paginate(query, page: int, limit: int) -> Tuple[List[Any], Dict[str, int]]:
    page, limit = page_params(page, limit)
    total = query.order_by(None).count()
    rows = query.offset((page - 1) * limit).limit(limit).all()
    return rows, {"page": page, "limit": limit, "total": total,
                  "total_pages": math.ceil(total / limit) if total else 0}


def get_scoped(db, model, row_id: int, ctx: AuthCtx, soft_delete: bool = True):
    q = db.query(model).filter(model.id == row_id)
    if soft_delete and hasattr(model, "deleted_at"):
        q = q.filter(model.deleted_at.is_(None))
    row = q.first()
    if row is None:
        raise HTTPException(404, "Not found")
    assert_row_in_scope(row.organization_id, ctx)
    return row
