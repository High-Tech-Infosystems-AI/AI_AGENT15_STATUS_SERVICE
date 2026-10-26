"""Events on/off — /status/clawback-notifications/events (contract §6.2).

Permissions: clawback_notifications.view (list) /
clawback_notifications.manage (toggle).

rule:<id> / digest:<id> toggles go through the existing /toggle handlers so
the behaviour (overlap check on enable, audit fields) is identical.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import events as EV
from app.whatsapp_layer.api import rules_api
from app.whatsapp_layer.api.common import get_scoped, read_org, write_org
from app.whatsapp_layer.models import ClawbackDigestRule, ClawbackNotificationRule
from app.whatsapp_layer.schemas import EventToggle, Toggle

logger = logging.getLogger("app_logger")
router = APIRouter()

VIEW = rules_api.VIEW
MANAGE = rules_api.MANAGE


@router.get("/events")
def list_events(organization_id: Optional[int] = Query(None),
                ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    return {"data": EV.list_events(db, read_org(organization_id, ctx))}


@router.patch("/events/{key}")
def toggle_event(key: str, body: EventToggle, organization_id: Optional[int] = Query(None),
                 ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    try:
        kind, ref = EV.parse_key(key)
    except EV.EventNotFound:
        raise HTTPException(404, "Unknown event")

    if kind == "rule":
        rules_api.toggle_rule(ref, Toggle(is_active=body.enabled), ctx, db)
        r = get_scoped(db, ClawbackNotificationRule, ref, ctx)
        return EV.rule_event(r, rules_api._templates_by_id(db, [r.template_id]).get(r.template_id))
    if kind == "digest":
        rules_api.toggle_digest_rule(ref, Toggle(is_active=body.enabled), ctx, db)
        r = get_scoped(db, ClawbackDigestRule, ref, ctx)
        return EV.digest_event(r, rules_api._templates_by_id(db, [r.template_id]).get(r.template_id))

    org = write_org(organization_id, body.organization_id, ctx)
    st = E.get_settings(db, org)
    EV.set_system_toggle(st, ref, body.enabled, ctx.user_id)
    db.commit()
    db.refresh(st)
    logger.info("Clawback event system:%s for org %s → %s (user %s)",
                ref, org, "on" if body.enabled else "off", ctx.user_id)
    return EV.system_event(db, org, st, ref)
