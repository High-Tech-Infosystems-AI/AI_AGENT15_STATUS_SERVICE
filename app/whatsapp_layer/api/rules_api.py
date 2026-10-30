"""Clawback notification settings, readiness, recruiter rules, digest rules,
reset-defaults and simulate — /status/clawback-notifications/* (contract
§2.5–§2.8).

Permissions: clawback_notifications.view (reads, simulate) /
clawback_notifications.manage (writes).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.whatsapp_layer import accounts as acct_svc, clients, people, seeds
from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import digest_engine as D
from app.whatsapp_layer.api.common import get_scoped, read_org, unprocessable, write_org
from app.whatsapp_layer.models import (
    ClawbackDigestRule, ClawbackHealthCheck, ClawbackNotificationRule, WhatsAppTemplate as T,
)
from app.whatsapp_layer.schemas import DigestRuleWrite, RuleWrite, SettingsUpdate, Toggle

logger = logging.getLogger("app_logger")
router = APIRouter()

VIEW = "clawback_notifications.view"
MANAGE = "clawback_notifications.manage"
REQUIRED_PURPOSES = ["recruiter_reminder", "recruiter_batch", "final_validation", "team_digest"]
RULE_TEMPLATE_PURPOSES = ("recruiter_reminder", "final_validation", "generic")
DIGEST_TEMPLATE_PURPOSES = ("team_digest", "generic")


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------
def _template_ref(tpl: Optional[T]) -> Optional[Dict[str, Any]]:
    if tpl is None:
        return None
    return {"id": tpl.id, "name": tpl.name, "approval_status": tpl.approval_status}


def _templates_by_id(db: Session, ids) -> Dict[int, T]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {t.id: t for t in db.query(T).filter(T.id.in_(ids), T.deleted_at.is_(None)).all()}


def serialize_rule(r: ClawbackNotificationRule, tpl: Optional[T]) -> Dict[str, Any]:
    return {
        "id": r.id, "name": r.name, "stage_label": r.stage_label,
        "action_text": r.action_text, "anchor": r.anchor,
        "start_day": r.start_day, "end_day": r.end_day, "frequency_type": r.frequency_type,
        "frequency_value": r.frequency_value or {}, "send_time": r.send_time,
        "recipient_type": r.recipient_type, "template_id": r.template_id,
        "template": _template_ref(tpl), "creates_health_check": bool(r.creates_health_check),
        "priority": r.priority, "is_active": bool(r.is_active),
    }


def serialize_digest(r: ClawbackDigestRule, tpl: Optional[T]) -> Dict[str, Any]:
    return {
        "id": r.id, "name": r.name, "audience_type": r.audience_type,
        "audience_value": r.audience_value or {}, "scope": r.scope,
        "frequency_type": r.frequency_type, "frequency_value": r.frequency_value or {},
        "send_time": r.send_time, "template_id": r.template_id, "template": _template_ref(tpl),
        "sections": list(r.sections or D.SECTIONS), "skip_if_empty": bool(r.skip_if_empty),
        "priority": r.priority, "is_active": bool(r.is_active),
    }


def _rule_out(db, r):
    return serialize_rule(r, _templates_by_id(db, [r.template_id]).get(r.template_id))


def _digest_out(db, r):
    return serialize_digest(r, _templates_by_id(db, [r.template_id]).get(r.template_id))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _validate_settings(data: Dict[str, Any]) -> List[Dict[str, str]]:
    errs = []
    if data.get("timezone"):
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            errs.append({"field": "timezone", "message": "Unknown timezone"})
    for f in ("quiet_hours_start", "quiet_hours_end", "missed_check_cutoff_time"):
        if data.get(f) not in (None, "") and E.parse_hhmm(data[f]) is None:
            errs.append({"field": f, "message": "Use HH:MM (24h)"})
    wd = data.get("working_days")
    if wd is not None and (not wd or any(str(d).upper() not in E.WEEKDAYS for d in wd)):
        errs.append({"field": "working_days", "message": "Use MON..SUN (at least one)"})
    url = data.get("app_base_url")
    if url and not str(url).startswith(("http://", "https://")):
        errs.append({"field": "app_base_url", "message": "Must start with http:// or https://"})
    return errs


@router.get("/settings")
def get_settings(organization_id: Optional[int] = Query(None),
                 ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    return E.settings_dict(E.get_settings(db, read_org(organization_id, ctx)))


@router.put("/settings")
def update_settings(body: SettingsUpdate, organization_id: Optional[int] = Query(None),
                    ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    org = write_org(organization_id, body.organization_id, ctx)
    data = body.model_dump(exclude_unset=True, exclude={"organization_id"})
    unprocessable(_validate_settings(data))
    st = E.get_settings(db, org)
    was_enabled = bool(st.enabled)
    # Nullable: empty clears (e.g. no quiet hours).
    for f in ("quiet_hours_start", "quiet_hours_end", "app_base_url"):
        if f in data:
            setattr(st, f, data[f] or None)
    # NOT NULL columns: ignore explicit nulls.
    for f in ("timezone", "recruiter_batch_threshold", "missed_check_cutoff_time"):
        if data.get(f) is not None:
            setattr(st, f, data[f])
    if "working_days" in data:
        st.working_days = [str(d).upper() for d in data["working_days"]]
    for f in ("enabled", "fallback_to_created_by", "digest_skip_if_empty"):
        if f in data and data[f] is not None:
            setattr(st, f, 1 if data[f] else 0)
    st.updated_by = ctx.user_id

    # First enable with no rules → seed the defaults (contract §4).
    if st.enabled and not was_enabled and st.defaults_seeded_at is None:
        has_rules = db.query(ClawbackNotificationRule.id).filter(
            ClawbackNotificationRule.organization_id == org,
            ClawbackNotificationRule.deleted_at.is_(None)).first()
        if not has_rules:
            seeds.seed_defaults(db, org, ctx.user_id)
        st.defaults_seeded_at = datetime.utcnow()
    db.commit()
    db.refresh(st)
    return E.settings_dict(st)


@router.get("/readiness")
def readiness(organization_id: Optional[int] = Query(None),
              ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    org = read_org(organization_id, ctx)
    st = E.get_settings(db, org)
    account = acct_svc.get_any_account(db, org)

    tpls = db.query(T).filter(T.organization_id == org, T.deleted_at.is_(None),
                              T.is_active == 1).all()
    approved_purposes = {t.purpose for t in tpls if t.approval_status == "approved"}
    rules = db.query(ClawbackNotificationRule).filter(
        ClawbackNotificationRule.organization_id == org, ClawbackNotificationRule.is_active == 1,
        ClawbackNotificationRule.deleted_at.is_(None)).all()
    digests = D.active_digest_rules(db, org)
    tmap = {t.id: t for t in tpls}
    unapproved = sum(1 for r in list(rules) + list(digests)
                     if not tmap.get(r.template_id) or tmap[r.template_id].approval_status != "approved")

    # Recruiters: owners of active clawbacks (job service; health checks as fallback).
    recruiter_ids = set()
    try:
        items = clients.fetch_active_clawbacks(org).get("items") or []
        recruiter_ids = {E.recruiter_for(i, bool(st.fallback_to_created_by)) for i in items} - {None}
    except Exception as exc:
        logger.warning("Readiness: job service unavailable (%s); using health checks", exc)
        since = datetime.utcnow().date() - timedelta(days=30)
        recruiter_ids = {r[0] for r in db.query(ClawbackHealthCheck.recruiter_user_id).filter(
            ClawbackHealthCheck.organization_id == org,
            ClawbackHealthCheck.due_date >= since).distinct().all() if r[0]}
    # Managers: everyone a digest rule would reach (org service, best-effort).
    manager_ids = set()
    for rule in (D.as_digest_spec(r) for r in digests):
        try:
            req = D.audience_request(rule)
            audience = req.pop("audience")
            manager_ids |= {int(u["user_id"]) for u in
                            clients.fetch_subordinates(org, audience, **req) if u.get("user_id")}
        except Exception as exc:
            logger.warning("Readiness: org service lookup failed for digest rule %s: %s", rule.id, exc)

    users = people.get_users(db, recruiter_ids | manager_ids)
    missing = []
    for uid in sorted(recruiter_ids | manager_ids):
        u = users.get(uid)
        if u and u["enabled"] and not u["phone_e164"]:
            missing.append({"user_id": uid, "name": u["name"], "email": u["email"],
                            "kind": "recruiter" if uid in recruiter_ids else "manager"})
    return {
        "enabled": bool(st.enabled),
        "account": {"connected": account is not None,
                    "status": account.status if account else None},
        "templates": {
            "approved": sum(1 for t in tpls if t.approval_status == "approved"),
            "pending": sum(1 for t in tpls if t.approval_status == "pending"),
            "missing_purposes": [p for p in REQUIRED_PURPOSES if p not in approved_purposes],
        },
        "rules": {"active": len(rules) + len(digests), "using_unapproved_template": unapproved},
        "users_missing_phone": missing,
    }


# ---------------------------------------------------------------------------
# Recruiter rules
# ---------------------------------------------------------------------------
def _check_template(db: Session, org: int, template_id: Optional[int], purposes) -> None:
    if template_id is None:
        return
    tpl = db.query(T).filter(T.id == template_id, T.organization_id == org,
                             T.deleted_at.is_(None)).first()
    if tpl is None:
        unprocessable([{"field": "template_id", "message": "Template not found"}])
    if tpl.purpose not in purposes:
        unprocessable([{"field": "template_id",
                        "message": f"Template purpose must be one of {', '.join(purposes)}"}])


def _check_overlap(db: Session, org: int, spec: E.RuleSpec) -> None:
    others = [E.as_rule_spec(r) for r in E.active_rules(db, org)]
    clash = E.find_overlap(spec, others)
    if clash:
        unprocessable([{"field": "start_day", "message": f"Overlaps with rule '{clash.name}'"}])


def _rule_data(body: RuleWrite) -> Dict[str, Any]:
    data = body.model_dump(exclude={"organization_id"})
    data["frequency_value"] = data.get("frequency_value") or {}
    if data["frequency_type"] == "weekdays":
        data["frequency_value"]["weekdays"] = [str(w).upper() for w in
                                               data["frequency_value"].get("weekdays") or []]
    return data


def _apply_rule(r: ClawbackNotificationRule, data: Dict[str, Any]) -> None:
    for f in ("name", "anchor", "start_day", "end_day", "frequency_type", "frequency_value",
              "send_time", "recipient_type", "template_id", "priority"):
        setattr(r, f, data[f])
    r.stage_label = data.get("stage_label") or data["name"]
    r.action_text = (data.get("action_text") or "").strip() or None
    r.send_time = E.parse_hhmm(data["send_time"]).strftime("%H:%M")
    r.creates_health_check = 1 if data["creates_health_check"] else 0
    r.is_active = 1 if data["is_active"] else 0


@router.get("/rules")
def list_rules(organization_id: Optional[int] = Query(None),
               ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    org = read_org(organization_id, ctx)
    rows = db.query(ClawbackNotificationRule).filter(
        ClawbackNotificationRule.organization_id == org,
        ClawbackNotificationRule.deleted_at.is_(None)).all()
    rows.sort(key=lambda r: (E.ANCHORS.index(r.anchor) if r.anchor in E.ANCHORS else 9,
                             r.start_day, r.id))
    tmap = _templates_by_id(db, [r.template_id for r in rows])
    return {"data": [serialize_rule(r, tmap.get(r.template_id)) for r in rows]}


@router.post("/rules")
def create_rule(body: RuleWrite, organization_id: Optional[int] = Query(None),
                ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    org = write_org(organization_id, body.organization_id, ctx)
    data = _rule_data(body)
    unprocessable(E.validate_rule_fields(data))
    _check_template(db, org, data["template_id"], RULE_TEMPLATE_PURPOSES)
    _check_overlap(db, org, E.as_rule_spec(data))
    r = ClawbackNotificationRule(organization_id=org, created_by=ctx.user_id, updated_by=ctx.user_id)
    _apply_rule(r, data)
    db.add(r)
    db.commit()
    db.refresh(r)
    return _rule_out(db, r)


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleWrite, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    r = get_scoped(db, ClawbackNotificationRule, rule_id, ctx)
    data = _rule_data(body)
    unprocessable(E.validate_rule_fields(data))
    _check_template(db, r.organization_id, data["template_id"], RULE_TEMPLATE_PURPOSES)
    _check_overlap(db, r.organization_id, E.as_rule_spec({**data, "id": r.id}))
    _apply_rule(r, data)
    r.updated_by = ctx.user_id
    db.commit()
    db.refresh(r)
    return _rule_out(db, r)


@router.patch("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int, body: Toggle, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    r = get_scoped(db, ClawbackNotificationRule, rule_id, ctx)
    if body.is_active and not r.is_active:
        spec = E.as_rule_spec(r)
        spec.is_active = True
        _check_overlap(db, r.organization_id, spec)
    r.is_active = 1 if body.is_active else 0
    r.updated_by = ctx.user_id
    db.commit()
    db.refresh(r)
    return _rule_out(db, r)


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    r = get_scoped(db, ClawbackNotificationRule, rule_id, ctx)
    r.deleted_at, r.is_active, r.updated_by = datetime.utcnow(), 0, ctx.user_id
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Digest rules
# ---------------------------------------------------------------------------
def _digest_data(body: DigestRuleWrite) -> Dict[str, Any]:
    data = body.model_dump(exclude={"organization_id"})
    data["audience_value"] = data.get("audience_value") or {}
    data["frequency_value"] = data.get("frequency_value") or {}
    fv = data["frequency_value"]
    if "weekday" in fv:
        fv["weekday"] = str(fv["weekday"]).upper()
    if "weekdays" in fv:
        fv["weekdays"] = [str(w).upper() for w in fv["weekdays"] or []]
    return data


def _apply_digest(r: ClawbackDigestRule, data: Dict[str, Any], default_skip: bool) -> None:
    for f in ("name", "audience_type", "audience_value", "scope", "frequency_type",
              "frequency_value", "template_id", "priority"):
        setattr(r, f, data[f])
    r.send_time = E.parse_hhmm(data["send_time"]).strftime("%H:%M")
    r.sections = data.get("sections") or list(D.SECTIONS)
    skip = data.get("skip_if_empty")
    r.skip_if_empty = 1 if (default_skip if skip is None else skip) else 0
    r.is_active = 1 if data["is_active"] else 0


@router.get("/digest-rules")
def list_digest_rules(organization_id: Optional[int] = Query(None),
                      ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    org = read_org(organization_id, ctx)
    rows = (db.query(ClawbackDigestRule)
            .filter(ClawbackDigestRule.organization_id == org,
                    ClawbackDigestRule.deleted_at.is_(None))
            .order_by(ClawbackDigestRule.priority.desc(), ClawbackDigestRule.id.asc()).all())
    tmap = _templates_by_id(db, [r.template_id for r in rows])
    return {"data": [serialize_digest(r, tmap.get(r.template_id)) for r in rows]}


@router.post("/digest-rules")
def create_digest_rule(body: DigestRuleWrite, organization_id: Optional[int] = Query(None),
                       ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    org = write_org(organization_id, body.organization_id, ctx)
    data = _digest_data(body)
    unprocessable(D.validate_digest_fields(data))
    _check_template(db, org, data["template_id"], DIGEST_TEMPLATE_PURPOSES)
    st = E.get_settings(db, org)
    r = ClawbackDigestRule(organization_id=org, created_by=ctx.user_id, updated_by=ctx.user_id)
    _apply_digest(r, data, bool(st.digest_skip_if_empty))
    db.add(r)
    db.commit()
    db.refresh(r)
    return _digest_out(db, r)


@router.put("/digest-rules/{rule_id}")
def update_digest_rule(rule_id: int, body: DigestRuleWrite, ctx: AuthCtx = Depends(get_auth_ctx),
                       db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    r = get_scoped(db, ClawbackDigestRule, rule_id, ctx)
    data = _digest_data(body)
    unprocessable(D.validate_digest_fields(data))
    _check_template(db, r.organization_id, data["template_id"], DIGEST_TEMPLATE_PURPOSES)
    _apply_digest(r, data, bool(r.skip_if_empty))
    r.updated_by = ctx.user_id
    db.commit()
    db.refresh(r)
    return _digest_out(db, r)


@router.patch("/digest-rules/{rule_id}/toggle")
def toggle_digest_rule(rule_id: int, body: Toggle, ctx: AuthCtx = Depends(get_auth_ctx),
                       db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    r = get_scoped(db, ClawbackDigestRule, rule_id, ctx)
    r.is_active = 1 if body.is_active else 0
    r.updated_by = ctx.user_id
    db.commit()
    db.refresh(r)
    return _digest_out(db, r)


@router.delete("/digest-rules/{rule_id}", status_code=204)
def delete_digest_rule(rule_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                       db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    r = get_scoped(db, ClawbackDigestRule, rule_id, ctx)
    r.deleted_at, r.is_active, r.updated_by = datetime.utcnow(), 0, ctx.user_id
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Reset defaults / simulate
# ---------------------------------------------------------------------------
@router.post("/reset-defaults")
def reset_defaults(organization_id: Optional[int] = Query(None),
                   ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(MANAGE)
    org = write_org(organization_id, None, ctx)
    st = E.get_settings(db, org)
    rules, digests = seeds.seed_defaults(db, org, ctx.user_id, replace=True)
    st.defaults_seeded_at = datetime.utcnow()
    db.commit()
    tmap = _templates_by_id(db, [r.template_id for r in rules + digests])
    return {"rules": [serialize_rule(r, tmap.get(r.template_id)) for r in rules],
            "digest_rules": [serialize_digest(r, tmap.get(r.template_id)) for r in digests]}


@router.get("/simulate")
def simulate(candidate_job_id: int = Query(...), days: int = Query(120, ge=1, le=E.SIMULATE_MAX_DAYS),
             organization_id: Optional[int] = Query(None),
             ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    org = read_org(organization_id, ctx)
    try:
        return E.simulate(db, org, candidate_job_id, days)
    except E.ClawbackNotFound as exc:
        raise HTTPException(404, str(exc))
    except clients.ServiceClientError as exc:
        raise HTTPException(502, str(exc))
