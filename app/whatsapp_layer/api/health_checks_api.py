"""Clawback health checks, clawback summary and team health —
/status/clawback-notifications/health-checks, /clawback-summary, /team-health
(contract §2.9).

Visibility: admin tier sees the whole org; everyone else sees their own
checks plus their hierarchy subordinates' (Org Service §3.2). If the Org
Service is down, non-admins fall back to their own checks only.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.whatsapp_layer import clients, people
from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import digest_engine as D
from app.whatsapp_layer.api.common import get_scoped, paginate, read_org, unprocessable
from app.whatsapp_layer.models import ClawbackHealthCheck as H, WhatsAppMessageLog as L
from app.whatsapp_layer.schemas import HealthCheckRecord

logger = logging.getLogger("app_logger")
router = APIRouter()

VIEW = "clawback_health_checks.view"
RECORD = "clawback_health_checks.record"
OUTCOMES = ("engaged", "at_risk", "not_reachable", "dropped")
PERIOD_DAYS = {"today": 1, "7d": 7, "30d": 30}


def _subordinates(org: int, user_id: int) -> Set[int]:
    try:
        rec = clients.subordinates_of(org, user_id)
        return {int(x) for x in (rec or {}).get("subordinate_user_ids") or []}
    except Exception as exc:
        logger.warning("Org service subordinates lookup failed for user %s: %s", user_id, exc)
        return set()


def visible_recruiters(ctx: AuthCtx, org: int) -> Optional[Set[int]]:
    """None = whole org (admin tier); else self + subordinates."""
    if ctx.is_admin_tier:
        return None
    return {int(ctx.user_id)} | _subordinates(org, ctx.user_id)


def _iso(dt) -> Optional[str]:
    return dt.replace(microsecond=0).isoformat() + "Z" if isinstance(dt, datetime) else None


def serialize_checks(db: Session, rows: List[H]) -> List[Dict[str, Any]]:
    names = people.user_names(db, [r.recruiter_user_id for r in rows] + [r.recorded_by for r in rows])
    return [{
        "id": r.id, "candidate_job_id": r.candidate_job_id,
        "candidate_job_status_id": r.candidate_job_status_id, "candidate_id": r.candidate_id,
        "candidate_name": r.candidate_name, "job_id": r.job_id, "job_title": r.job_title,
        "company_name": r.company_name, "recruiter_user_id": r.recruiter_user_id,
        "recruiter_name": names.get(r.recruiter_user_id), "rule_id": r.rule_id,
        "stage_label": r.stage_label, "day_number": r.day_number,
        "due_date": r.due_date.isoformat() if r.due_date else None, "status": r.status,
        "outcome": r.outcome, "notes": r.notes, "recorded_by": r.recorded_by,
        "recorded_by_name": names.get(r.recorded_by), "recorded_at": _iso(r.recorded_at),
    } for r in rows]


def _today(db: Session, org: int) -> date:
    st = E.get_settings(db, org)
    return E.local_now(st.timezone).date()


@router.get("/health-checks")
def list_health_checks(status: Optional[str] = Query(None),
                       due_from: Optional[date] = Query(None), due_to: Optional[date] = Query(None),
                       job_id: Optional[int] = Query(None),
                       candidate_job_id: Optional[int] = Query(None),
                       recruiter_user_id: Optional[int] = Query(None),
                       mine: bool = Query(False),
                       page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100),
                       organization_id: Optional[int] = Query(None),
                       ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    org = read_org(organization_id, ctx)
    q = db.query(H).filter(H.organization_id == org)
    if mine:
        q = q.filter(H.recruiter_user_id == ctx.user_id)
    else:
        visible = visible_recruiters(ctx, org)
        if visible is not None:
            q = q.filter(H.recruiter_user_id.in_(list(visible)))
    if status:
        q = q.filter(H.status == status)
    if due_from:
        q = q.filter(H.due_date >= due_from)
    if due_to:
        q = q.filter(H.due_date <= due_to)
    if job_id:
        q = q.filter(H.job_id == job_id)
    if candidate_job_id:
        q = q.filter(H.candidate_job_id == candidate_job_id)
    if recruiter_user_id:
        q = q.filter(H.recruiter_user_id == recruiter_user_id)
    rows, pagination = paginate(q.order_by(H.due_date.desc(), H.id.desc()), page, limit)
    return {"data": serialize_checks(db, rows), "pagination": pagination}


@router.put("/health-checks/{check_id}")
def record_health_check(check_id: int, body: HealthCheckRecord,
                        ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(RECORD)
    chk = get_scoped(db, H, check_id, ctx, soft_delete=False)
    if body.outcome not in OUTCOMES:
        unprocessable([{"field": "outcome", "message": f"Must be one of {', '.join(OUTCOMES)}"}])
    if not ctx.is_admin_tier and chk.recruiter_user_id != ctx.user_id:
        if chk.recruiter_user_id not in _subordinates(chk.organization_id, ctx.user_id):
            raise HTTPException(403, "Only the recruiter, their managers or an admin can record this check")
    if chk.status == "cancelled":
        raise HTTPException(409, "This health check was cancelled")
    chk.status = "done"
    chk.outcome = body.outcome
    chk.notes = body.notes
    chk.recorded_by = ctx.user_id
    chk.recorded_at = datetime.utcnow()
    db.commit()
    db.refresh(chk)
    return serialize_checks(db, [chk])[0]


@router.get("/clawback-summary")
def clawback_summary(candidate_job_ids: str = Query(...),
                     organization_id: Optional[int] = Query(None),
                     ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    org = read_org(organization_id, ctx)
    try:
        ids = sorted({int(x) for x in candidate_job_ids.split(",") if x.strip()})
    except ValueError:
        unprocessable([{"field": "candidate_job_ids", "message": "Comma-separated integers"}])
    if len(ids) > 500:
        unprocessable([{"field": "candidate_job_ids", "message": "At most 500 ids"}])
    if not ids:
        return {"items": []}
    today = _today(db, org)

    try:
        items = {int(i["candidate_job_id"]): i for i in
                 clients.fetch_active_clawbacks(org).get("items") or []
                 if int(i.get("candidate_job_id") or 0) in ids}
    except Exception as exc:
        logger.warning("clawback-summary: job service unavailable (%s); next_reminder_date omitted", exc)
        items = {}
    rules = [E.as_rule_spec(r) for r in E.active_rules(db, org)]

    logs = (db.query(L).filter(L.organization_id == org, L.candidate_job_id.in_(ids))
            .order_by(L.id.desc()).all())
    last_log: Dict[int, L] = {}
    sent_today: Set[int] = set()
    for lg in logs:
        last_log.setdefault(lg.candidate_job_id, lg)
    checks = db.query(H).filter(H.organization_id == org, H.candidate_job_id.in_(ids)).all()
    today_check = {c.candidate_job_id: c for c in checks if c.due_date == today}
    open_checks: Dict[int, int] = {}
    for c in checks:
        if c.status in ("pending", "missed"):
            open_checks[c.candidate_job_id] = open_checks.get(c.candidate_job_id, 0) + 1
        if c.due_date == today:
            sent_today.add(c.candidate_job_id)

    out = []
    for cj in ids:
        nxt = None
        if cj in items:
            # Today's reminder is "next" until today's check exists.
            start = today + timedelta(days=1) if cj in sent_today else today
            d = E.next_reminder_date(rules, items[cj], start)
            nxt = d.isoformat() if d else None
        lg, chk = last_log.get(cj), today_check.get(cj)
        out.append({
            "candidate_job_id": cj,
            "next_reminder_date": nxt,
            "last_message": ({"id": lg.id, "status": lg.status,
                              "sent_at": _iso(lg.sent_at or lg.submitted_at)} if lg else None),
            "today_check": ({"id": chk.id, "status": chk.status, "outcome": chk.outcome}
                            if chk else None),
            "open_checks": open_checks.get(cj, 0),
        })
    return {"items": out}


@router.get("/team-health")
def team_health(period: str = Query("7d"), organization_id: Optional[int] = Query(None),
                ctx: AuthCtx = Depends(get_auth_ctx), db: Session = Depends(get_db)):
    ctx.require(VIEW)
    if period not in PERIOD_DAYS:
        unprocessable([{"field": "period", "message": "Must be today, 7d or 30d"}])
    org = read_org(organization_id, ctx)
    today = _today(db, org)
    start = today - timedelta(days=PERIOD_DAYS[period] - 1)
    scope = visible_recruiters(ctx, org)

    try:
        items = clients.fetch_active_clawbacks(org).get("items") or []
    except Exception as exc:
        logger.warning("team-health: job service unavailable (%s); clawback counts omitted", exc)
        items = []
    rules = [E.as_rule_spec(r) for r in E.active_rules(db, org)]
    checks = D.check_dicts(db, org, start)
    agg = D.aggregate(scope, items, checks, rules, today, start)

    if scope is None:
        rec_ids = set(agg["recruiters"]) | {E.recruiter_for(i, True) for i in items} - {None}
        label = f"Whole organisation ({len(rec_ids)} recruiter{'s' if len(rec_ids) != 1 else ''})"
    else:
        label = D.scope_label(len(scope - {ctx.user_id}))
    names = people.user_names(db, list(agg["recruiters"]))
    recruiters = sorted(({"user_id": uid, "name": names.get(uid), **v}
                         for uid, v in agg["recruiters"].items()),
                        key=lambda r: (-r["missed"], -r["due"], r["name"] or ""))
    at_risk_rows = (db.query(H).filter(H.id.in_(agg["at_risk_check_ids"]))
                    .order_by(H.due_date.desc()).all() if agg["at_risk_check_ids"] else [])
    rec_names = people.user_names(db, [E.recruiter_for(i, True) for i in agg["ending_soon_items"]])
    ending = sorted(({"candidate_job_id": i.get("candidate_job_id"),
                      "candidate_name": i.get("candidate_name"), "job_title": i.get("job_title"),
                      "clawback_completion_date": str(E.completion_date_of(i)),
                      "recruiter_name": rec_names.get(E.recruiter_for(i, True))}
                     for i in agg["ending_soon_items"]),
                    key=lambda x: x["clawback_completion_date"])
    return {"scope_label": label, "period": period, "totals": agg["totals"],
            "by_stage": agg["by_stage"], "recruiters": recruiters,
            "at_risk": serialize_checks(db, at_risk_rows), "ending_soon": ending}
