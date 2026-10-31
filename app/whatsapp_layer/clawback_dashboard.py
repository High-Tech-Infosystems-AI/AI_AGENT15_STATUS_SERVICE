"""Clawback dashboard (contract §6.8): every clawback case in scope, with alerts.

Recruiter ownership and hierarchy match Today's check-ins. The status service
shares the database, so cases are read directly, like `people`.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import today as TL

MAX_CASES = 5000
ENDING_SOON_DAYS = 7
ALERT_KINDS = ("overdue", "due_today", "at_risk", "ending_soon", "final_pending")
# Period length buckets, shortest first: (longest period in days, label).
PERIOD_BUCKETS = ((1, "1 day"), (10, "2-10 days"), (20, "11-20 days"), (30, "21-30 days"),
                  (40, "31-40 days"), (50, "41-50 days"), (60, "51-60 days"), (70, "61-70 days"),
                  (80, "71-80 days"), (85, "81-85 days"), (90, "86-90 days"))
UNDER_A_DAY = "<1 day"
OVER_90 = "90+ days"

_CASES_SQL = text("""
    SELECT cj.id, cj.job_id, c.candidate_id, c.candidate_name, c.assigned_to, c.created_by,
           j.title, j.company_id, co.company_name, j.cooling_period, s.joined_at,
           s.cooling_period_closed,
           (SELECT d.type FROM candidate_job_status d WHERE d.candidate_job_id = cj.id
             AND d.type IN ('rejected', 'dropped') ORDER BY d.id DESC LIMIT 1) AS ended_type,
           (SELECT COALESCE(d.rejected_at, d.created_at) FROM candidate_job_status d
             WHERE d.candidate_job_id = cj.id AND d.type IN ('rejected', 'dropped')
             ORDER BY d.id DESC LIMIT 1) AS ended_at
      FROM candidate_job_status s
      JOIN candidate_jobs cj ON cj.id = s.candidate_job_id
      JOIN job_openings j ON j.id = cj.job_id
      LEFT JOIN candidates c ON c.candidate_id = cj.candidate_id
      LEFT JOIN companies co ON co.id = j.company_id
     WHERE s.type = 'joined' AND s.joined_at IS NOT NULL AND j.organization_id = :org
       AND j.deleted_at IS NULL AND j.cooling_period > 0
     ORDER BY s.joined_at DESC, s.id DESC LIMIT :limit
""")


def fetch_case_rows(db: Session, org: int) -> List[Dict[str, Any]]:
    rows = db.execute(_CASES_SQL, {"org": org, "limit": MAX_CASES}).mappings().all()
    seen: Set[int] = set()
    out = []
    for r in rows:
        if r["id"] not in seen:      # the latest joined row of each candidate_job
            seen.add(r["id"])
            out.append(dict(r))
    return out


def period_bucket(days: float) -> str:
    """Bucket for a period length; part days go up (10.5 is in 11-20 days)."""
    if days < 1:
        return UNDER_A_DAY
    span = math.ceil(days)
    return next((label for top, label in PERIOD_BUCKETS if span <= top), OVER_90)


def _iso_date(value: Any) -> Optional[str]:
    parsed = E.parse_date(value)
    return parsed.isoformat() if parsed else None


def _case_status(row: Dict[str, Any], days_pending: Optional[int]) -> str:
    if row.get("ended_type"):
        return "dropped"
    if row.get("cooling_period_closed"):
        return "completed"
    return "cooling_ended" if days_pending == 0 else "in_cooling"


def build_case(row: Dict[str, Any], check: Optional[Dict[str, Any]], today: date,
               names: Dict[int, str]) -> Dict[str, Any]:
    joined = E.parse_date(row["joined_at"])
    item = {"joined_at": joined.isoformat() if joined else None,
            "cooling_period_days": float(row["cooling_period"] or 0),
            "offer_accepted_at": None, "clawback_completion_date": None}
    # Periods can be part of a day (0.5); they still end, on the next calendar day.
    span = math.ceil(item["cooling_period_days"])
    if joined and span > 0:
        item["clawback_completion_date"] = (joined + timedelta(days=span)).isoformat()
    progress = TL._progress(item, today)
    if progress["progress_percent"] is None and joined and span > 0:
        progress["progress_percent"] = 100 if progress["days_pending"] == 0 else 0
    status = _case_status(row, progress["days_pending"])
    if status in ("dropped", "completed"):
        progress["days_pending"] = 0 if status == "completed" else progress["days_pending"]
    if status == "completed":
        progress["progress_percent"] = 100
    rid = row.get("assigned_to") or row.get("created_by")
    check = check or {}
    ended = E.parse_date(row.get("ended_at")) if status == "dropped" else None
    drop_day = max((ended - joined).days + 1, 1) if ended and joined else None
    return {
        **progress,
        "candidate_job_id": row["id"], "candidate_id": row.get("candidate_id"),
        "candidate_name": row.get("candidate_name") or "-", "job_id": row["job_id"],
        "job_title": row.get("title") or "-", "company_id": row.get("company_id"),
        "company_name": row.get("company_name") or "-",
        "recruiter_user_id": int(rid) if rid else None,
        "recruiter_name": names.get(int(rid), "Unassigned") if rid else "Unassigned",
        "case_status": status,
        "period_bucket": period_bucket(item["cooling_period_days"]),
        # Which day after joining a dropped case ended on (joining date is day 1).
        "drop_day": drop_day,
        "drop_day_bucket": period_bucket(drop_day) if drop_day else None,
        # When the case finished, for the dashboard's date filter.
        "finished_at": _iso_date(row.get("ended_at") if status == "dropped"
                                 else row.get("cooling_period_closed") if status == "completed" else None),
        # Stops at the end of the period, however long a case stays open after it.
        "day_number": (min((today - joined).days + 1, span or 10 ** 6)
                       if joined else None),
        "progress": f"joined {joined.strftime('%d %b')}" if joined else "-",
        "check_id": check.get("id"), "status": check.get("status"),
        "outcome": check.get("outcome"), "notes": check.get("notes"),
        "due_date": check["due_date"].isoformat() if check.get("due_date") else None,
        "overdue": bool(check.get("due_date") and check["due_date"] < today
                        and check.get("status") in ("pending", "missed")),
        "action": check.get("stage_label") or "Check-in",
        "link": "",
    }


def alerts_for(case: Dict[str, Any], today: date) -> List[str]:
    """Why a case needs attention. Finished cases never alert."""
    if case["case_status"] in ("dropped", "completed"):
        return []
    out = []
    due = E.parse_date(case.get("due_date"))
    if case["status"] in ("pending", "missed") and due is not None:
        out.append("overdue" if due < today else "due_today")
    if case["outcome"] in ("at_risk", "not_reachable"):
        out.append("at_risk")
    if case["case_status"] == "cooling_ended":
        out.append("final_pending")
    elif case["days_pending"] is not None and case["days_pending"] <= ENDING_SOON_DAYS:
        out.append("ending_soon")
    return out


def _latest_checks(db: Session, org: int, today: date) -> Dict[int, Dict[str, Any]]:
    """Per candidate_job: the oldest open check, else the most recent one."""
    from app.whatsapp_layer.models import ClawbackHealthCheck as H
    out: Dict[int, Dict[str, Any]] = {}
    rows = (db.query(H).filter(H.organization_id == org, H.status != "cancelled")
            .order_by(H.due_date.desc(), H.id.desc()).all())
    for c in rows:
        rec = {"id": c.id, "status": c.status, "outcome": c.outcome, "notes": c.notes,
               "due_date": c.due_date, "stage_label": c.stage_label}
        cur = out.get(c.candidate_job_id)
        is_open = c.status in ("pending", "missed") and c.due_date <= today
        if cur is None or (is_open and (cur["status"] not in ("pending", "missed") or c.due_date < cur["due_date"])):
            # Keep what was learnt on the last finished call when one is still open.
            if cur is not None and cur.get("outcome") and not rec["outcome"]:
                rec["outcome"] = cur["outcome"]
            out[c.candidate_job_id] = rec
    return out


def build_dashboard(db: Session, org: int, scope: Optional[Set[int]]) -> Dict[str, Any]:
    """Cases in `scope` (None = whole org), offers waiting to join, and alerts."""
    from app.whatsapp_layer import clients, people
    st = E.get_settings(db, org)
    today = E.local_now(st.timezone).date()
    items = clients.fetch_active_clawbacks(org).get("items") or []
    rules = [E.as_rule_spec(r) for r in E.active_rules(db, org)]
    from app.whatsapp_layer import digest_engine as D
    D.due_today(db, org, items, rules, today, bool(st.fallback_to_created_by))   # today's checks
    db.commit()
    rows = fetch_case_rows(db, org)
    owner = lambda r: r.get("assigned_to") or r.get("created_by")
    rows = [r for r in rows if scope is None or (owner(r) and int(owner(r)) in scope)]
    names = people.user_names(db, {int(owner(r)) for r in rows if owner(r)})
    checks = _latest_checks(db, org, today)
    cases = []
    for r in rows:
        case = build_case(r, checks.get(r["id"]), today, names)
        case["alerts"] = alerts_for(case, today)
        cases.append(case)
    offers = [i for i in items if i.get("status") == "pre_joining"
              and (scope is None or (E.recruiter_for(i, True) or 0) in scope)]
    buckets = [UNDER_A_DAY, *(label for _, label in PERIOD_BUCKETS)]
    if any(c["period_bucket"] == OVER_90 for c in cases):    # only when someone is in it
        buckets.append(OVER_90)
    return {"date": today.isoformat(), "offer_accepted_count": len(offers),
            # What the dashboard's company, recruiter and job filters match on.
            "offers": [{"job_id": i.get("job_id"), "company_name": i.get("company_name") or "-",
                        "recruiter_user_id": E.recruiter_for(i, True)} for i in offers],
            "cases": cases,
            "period_buckets": buckets,
            "truncated": len(rows) >= MAX_CASES}
