"""WhatsApp templates + variable catalogue — /status/whatsapp/templates,
/status/whatsapp/variables (contract §2.2, §2.3).

Permissions: whatsapp_templates.view / whatsapp_templates.manage;
mark-approved is admin-tier only.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx, resolve_read_org
from app.whatsapp_layer import accounts as acct_svc, templates_service as ts
from app.whatsapp_layer import variables as V
from app.whatsapp_layer.api.common import get_scoped, paginate, unprocessable, write_org
from app.whatsapp_layer.models import WhatsAppTemplate as T
from app.whatsapp_layer.providers.base import ProviderError, ProviderUnsupported
from app.whatsapp_layer.providers.registry import get_provider
from app.whatsapp_layer.schemas import TemplateApprove, TemplatePreview, TemplateWrite

logger = logging.getLogger("app_logger")
router = APIRouter()

VIEW = "whatsapp_templates.view"
MANAGE = "whatsapp_templates.manage"


def _body_data(body: TemplateWrite) -> Dict[str, Any]:
    data = body.model_dump(exclude={"organization_id"})
    data["buttons"] = [b for b in (data.get("buttons") or [])]
    data["header_format"] = (data.get("header_format") or "NONE").upper()
    data["category"] = (data.get("category") or "UTILITY").upper()
    data["sample_values"] = {k: v for k, v in (data.get("sample_values") or {}).items()
                             if v is not None}
    return data


def _out(db: Session, tpl: T) -> Dict[str, Any]:
    return ts.serialize(tpl, ts.in_use_counts(db, [tpl.id]).get(tpl.id, 0))


def _check_name(db: Session, org: int, account_id: Optional[int], data: Dict[str, Any],
                exclude_id: Optional[int] = None) -> None:
    if ts.name_taken(db, org, account_id, data["name"], data["language"], exclude_id):
        unprocessable([{"field": "name",
                        "message": "A template with this name and language already exists"}])


@router.get("/variables")
def list_variables(ctx: AuthCtx = Depends(get_auth_ctx)):
    ctx.require(VIEW)
    return V.VARIABLES


@router.get("/templates")
def list_templates(purpose: Optional[str] = Query(None),
                   approval_status: Optional[str] = Query(None),
                   search: Optional[str] = Query(None),
                   page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100),
                   organization_id: Optional[int] = Query(None),
                   ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    org = resolve_read_org(organization_id, ctx)
    q = db.query(T).filter(T.deleted_at.is_(None))
    if org is not None:
        q = q.filter(T.organization_id == org)
    if purpose:
        q = q.filter(T.purpose == purpose)
    if approval_status:
        q = q.filter(T.approval_status == approval_status)
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(T.name.like(like), T.body_text_named.like(like)))
    rows, pagination = paginate(q.order_by(T.name.asc(), T.version.desc()), page, limit)
    counts = ts.in_use_counts(db, [r.id for r in rows])
    return {"data": [ts.serialize(r, counts.get(r.id, 0)) for r in rows], "pagination": pagination}


@router.get("/templates/{template_id}")
def get_template(template_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                 db: Session = Depends(get_db)):
    ctx.require(VIEW)
    return _out(db, get_scoped(db, T, template_id, ctx))


@router.post("/templates")
def create_template(body: TemplateWrite, organization_id: Optional[int] = Query(None),
                    ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    org = write_org(organization_id, body.organization_id, ctx)
    data = _body_data(body)
    unprocessable(ts.validate_template(data, for_submit=False))
    account = acct_svc.get_any_account(db, org)
    _check_name(db, org, account.id if account else None, data)
    tpl = T(organization_id=org, account_id=account.id if account else None,
            approval_status="draft", is_active=1, version=1, base_name=data["name"],
            created_by=ctx.user_id, updated_by=ctx.user_id)
    ts.apply_fields(tpl, data)
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return _out(db, tpl)


@router.put("/templates/{template_id}")
def update_template(template_id: int, body: TemplateWrite,
                    ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    tpl = get_scoped(db, T, template_id, ctx)
    if tpl.approval_status not in ts.EDITABLE_STATUSES:
        raise HTTPException(409, "Create a new version")
    data = _body_data(body)
    unprocessable(ts.validate_template(data, for_submit=False))
    _check_name(db, tpl.organization_id, tpl.account_id, data, exclude_id=tpl.id)
    ts.apply_fields(tpl, data)
    tpl.approval_status = "draft"
    tpl.updated_by = ctx.user_id
    db.commit()
    db.refresh(tpl)
    return _out(db, tpl)


@router.post("/templates/{template_id}/new-version")
def new_version(template_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    src = get_scoped(db, T, template_id, ctx)
    base = src.base_name or ts.base_name_of(src.name)
    max_version = (db.query(func.max(T.version))
                   .filter(T.organization_id == src.organization_id,
                           or_(T.base_name == base, T.name == base)).scalar()) or src.version or 1
    version = int(max_version) + 1
    name = ts.next_version_name(base, version)
    while ts.name_taken(db, src.organization_id, src.account_id, name, src.language):
        version += 1
        name = ts.next_version_name(base, version)
    tpl = T(organization_id=src.organization_id, account_id=src.account_id,
            approval_status="draft", is_active=1, version=version, base_name=base,
            parent_template_id=src.id, created_by=ctx.user_id, updated_by=ctx.user_id)
    ts.apply_fields(tpl, {**ts.template_data(src), "name": name})
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return _out(db, tpl)


@router.post("/templates/sync")
def sync_templates(organization_id: Optional[int] = Query(None),
                   ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    return ts.sync_templates(db, resolve_read_org(organization_id, ctx))


@router.post("/templates/{template_id}/submit")
def submit_template(template_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    tpl = get_scoped(db, T, template_id, ctx)
    if tpl.approval_status not in ts.EDITABLE_STATUSES:
        raise HTTPException(409, "Only draft or rejected templates can be submitted; create a new version")
    unprocessable(ts.validate_template(ts.template_data(tpl), for_submit=True))
    account = acct_svc.get_active_account(db, tpl.organization_id)
    if account is None:
        raise HTTPException(409, "Connect an active WhatsApp account before submitting templates")
    try:
        ts.submit(db, tpl, account, ctx.user_id)
    except ProviderError as exc:
        db.rollback()
        if exc.auth_error:
            acct_svc.mark_auth_error(db, account, exc.message)
        logger.warning("Template %s submit failed: %s", tpl.id, exc.message)
        raise HTTPException(502, f"Provider rejected the template: {exc.message}")
    except RuntimeError as exc:  # token decrypt / missing key
        db.rollback()
        raise HTTPException(500, str(exc))
    db.commit()
    db.refresh(tpl)
    return _out(db, tpl)


@router.post("/templates/{template_id}/mark-approved")
def mark_approved(template_id: int, body: TemplateApprove,
                  ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    """Manual override while the provider has no template status API."""
    ctx.require_admin_tier()
    tpl = get_scoped(db, T, template_id, ctx)
    previous = tpl.approval_status
    tpl.approval_status = "approved"
    tpl.rejection_reason = None
    tpl.approval_override_by = ctx.user_id
    tpl.approval_override_reason = body.reason.strip()[:500]
    tpl.approval_override_at = datetime.utcnow()
    tpl.updated_by = ctx.user_id
    db.commit()
    db.refresh(tpl)
    logger.warning("AUDIT: WhatsApp template %s (%s, org %s) manually marked approved by user %s "
                   "(was %s). Reason: %s", tpl.id, tpl.name, tpl.organization_id, ctx.user_id,
                   previous, tpl.approval_override_reason)
    return _out(db, tpl)


@router.post("/templates/{template_id}/preview")
def preview_template(template_id: int, body: Optional[TemplatePreview] = None,
                     ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    tpl = get_scoped(db, T, template_id, ctx)
    return ts.render_preview(tpl.header_format, tpl.header_text, tpl.body_text_named,
                             tpl.footer_text, tpl.buttons, (body.values if body else None),
                             tpl.sample_values)


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                    db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    tpl = get_scoped(db, T, template_id, ctx)
    if ts.in_use_counts(db, [tpl.id]).get(tpl.id, 0) > 0:
        raise HTTPException(409, "Template is used by notification rules")
    if tpl.provider_template_id and tpl.account_id:
        # Best effort; the provider delete API is UNCONFIRMED (doc §3.27 missing).
        account = acct_svc.get_active_account(db, tpl.organization_id)
        if account is not None:
            try:
                get_provider(account.provider).delete_template(acct_svc.credentials(account), tpl.name)
            except ProviderUnsupported:
                pass
            except Exception as exc:
                logger.warning("Provider delete for template %s failed: %s", tpl.id, exc)
    tpl.deleted_at = datetime.utcnow()
    tpl.is_active = 0
    tpl.updated_by = ctx.user_id
    db.commit()
    return Response(status_code=204)
