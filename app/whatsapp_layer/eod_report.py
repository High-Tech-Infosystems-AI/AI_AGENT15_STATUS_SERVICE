"""End-of-day report to team leads and admins (contract §6.9): what each recruiter
did today, which candidates weren't tracked, and who was dropped or is at risk.

Goes to the people the team summary rules reach, covering the same scope, once a
working day. Nobody is messaged on a day with nothing to report.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import digest_engine as D
from app.whatsapp_layer import variables as V

logger = logging.getLogger("app_logger")

EOD_TIME = "18:30"
TEMPLATE_NAME = "clawback_eod_report"
FLAG_OUTCOMES = {"dropped": "Dropped", "at_risk": "At risk", "not_reachable": "Not reachable"}
LIST_MAX_CHARS = 220

_DROPS_SQL = text("""
    SELECT cj.id, c.candidate_name, s.type, c.assigned_to, c.created_by
      FROM candidate_job_status s
      JOIN candidate_jobs cj ON cj.id = s.candidate_job_id
      JOIN job_openings j ON j.id = cj.job_id
      LEFT JOIN candidates c ON c.candidate_id = cj.candidate_id
     WHERE s.type IN ('rejected', 'dropped') AND j.organization_id = :org
       AND DATE(COALESCE(s.rejected_at, s.created_at)) = :day
""")


def fetch_drops(db: Session, org: int, today: date) -> List[Dict[str, Any]]:
    """Candidates dropped or rejected today, wherever it was done."""
    rows = db.execute(_DROPS_SQL, {"org": org, "day": today}).mappings().all()
    return [{"candidate_job_id": r["id"], "candidate_name": r["candidate_name"] or "-",
             "label": "Dropped" if r["type"] == "dropped" else "Rejected",
             "recruiter_user_id": r["assigned_to"] or r["created_by"]} for r in rows]


def checks_today(db: Session, org: int, today: date) -> List[Dict[str, Any]]:
    from app.whatsapp_layer.models import ClawbackHealthCheck as H
    rows = db.query(H).filter(H.organization_id == org, H.due_date == today,
                              H.status != "cancelled").all()
    return [{"candidate_job_id": c.candidate_job_id, "candidate_name": c.candidate_name or "-",
             "recruiter_user_id": c.recruiter_user_id, "status": c.status,
             "outcome": c.outcome} for c in rows]


def build_report(checks: List[Dict[str, Any]], drops: List[Dict[str, Any]],
                 scope: Optional[Set[int]], names: Dict[int, str]) -> Optional[Dict[str, str]]:
    """The report's variables for one recipient, or None when nothing happened."""
    def in_scope(rid) -> bool:
        return scope is None or (rid is not None and int(rid) in scope)

    mine = [c for c in checks if in_scope(c["recruiter_user_id"])]
    drops = [d for d in drops if in_scope(d["recruiter_user_id"])]
    if not mine and not drops:
        return None
    per: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for c in mine:
        per.setdefault(c["recruiter_user_id"], []).append(c)
    progress = sorted(
        (names.get(rid, "Unassigned"), sum(1 for c in cs if c["status"] == "done"), len(cs))
        for rid, cs in per.items())
    untracked = [c["candidate_name"] for c in mine if c["status"] != "done"]
    flagged = {d["candidate_job_id"]: f"{d['candidate_name']} ({d['label']})" for d in drops}
    for c in mine:
        if c["outcome"] in FLAG_OUTCOMES:
            flagged.setdefault(c["candidate_job_id"],
                               f"{c['candidate_name']} ({FLAG_OUTCOMES[c['outcome']]})")
    return {
        "done_count": str(sum(1 for c in mine if c["status"] == "done")),
        "total_count": str(len(mine)),
        "recruiter_progress": V.names_list([f"{n} {d} of {t}" for n, d, t in progress],
                                           LIST_MAX_CHARS) if progress else "None",
        "untracked_count": str(len(untracked)),
        "untracked_list": V.names_list(untracked, LIST_MAX_CHARS) if untracked else "None",
        "flagged_count": str(len(flagged)),
        "flagged_list": V.names_list(list(flagged.values()), LIST_MAX_CHARS) if flagged else "None",
    }


def run_org_eod(db: Session, org: int, now_utc: Optional[datetime] = None,
                dispatch=None) -> Dict[str, int]:
    from app.whatsapp_layer import accounts as acct_svc, clients, events as EV, people, sender
    from app.whatsapp_layer.models import WhatsAppMessageLog as L, WhatsAppTemplate as T
    stats = {"queued": 0, "skipped": 0, "empty": 0}
    st = E.get_settings(db, org)
    if not st or not st.enabled or not EV.system_enabled(st, EV.EOD_REPORT):
        return stats
    now_local = E.local_now(st.timezone, now_utc)
    today = now_local.date()
    if not E.is_working_day(today, st.working_days) or not E.time_reached(now_local, EOD_TIME):
        return stats

    rule_audiences = []
    for rule in map(D.as_digest_spec, D.active_digest_rules(db, org)):
        try:
            req = D.audience_request(rule)
            audience = req.pop("audience")
            users = (people.admin_users(db, org) if audience == "admins"
                     else clients.fetch_subordinates(org, audience, **req))
            rule_audiences.append((rule, users))
        except Exception as exc:
            db.rollback()
            logger.error("EOD: rule %s audience lookup failed: %s", rule.id, exc)
    ready = D.assign_recipients(rule_audiences)
    keys = {uid: E.idempotency_key("eod_report", 0, org, uid, today) for uid in ready}
    done = {k for (k,) in db.query(L.idempotency_key).filter(L.idempotency_key.in_(list(keys.values()) or [""])).all()}
    ready = {uid: ru for uid, ru in ready.items() if keys[uid] not in done}
    if not ready:
        return stats

    checks = checks_today(db, org, today)
    drops = fetch_drops(db, org, today)
    template = (db.query(T).filter(T.organization_id == org, T.name == TEMPLATE_NAME,
                                   T.deleted_at.is_(None)).first())
    account = acct_svc.get_active_account(db, org)
    users = people.get_users(db, list(ready))
    names = people.user_names(db, {int(r["recruiter_user_id"]) for r in checks + drops
                                   if r["recruiter_user_id"]})
    to_dispatch: List[int] = []
    for uid, (rule, record) in ready.items():
        org_wide = rule.scope == "organisation" or rule.audience_type == "admins"
        scope = None if org_wide else D.scope_user_ids(rule.scope, record) | {uid}
        report = build_report(checks, drops, scope, names)
        if report is None:
            stats["empty"] += 1
            continue
        user = users.get(uid) or {}
        log, created = sender.create_log(
            db, organization_id=org, kind="digest", idempotency_key=keys[uid],
            account_id=account.id if account else None,
            provider=account.provider if account else None, rule_name="End of day report",
            template_id=template.id if template else None,
            template_name=template.name if template else TEMPLATE_NAME, recipient_user_id=uid,
            recipient_name=user.get("name") or record.get("name"), recipient_type="manager",
            phone_e164=user.get("phone_e164"),
            scheduled_for=E.local_to_utc_naive(today, time(18, 30), st.timezone),
            params={"recipient_name": user.get("name") or record.get("name") or "-",
                    "report_date": today.strftime("%d %b"), **report,
                    "app_link": V.day_link(st.app_base_url, "team_digest", today)})
        if not created:
            continue
        sender.apply_prechecks(db, log, settings=st, account=account, template=template,
                               user=user or None)
        if log.status == "queued":
            to_dispatch.append(log.id)
            stats["queued"] += 1
        else:
            stats["skipped"] += 1
        db.commit()
    sender.dispatch_many(db, to_dispatch, dispatch)
    return stats
