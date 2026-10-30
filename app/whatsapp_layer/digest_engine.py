"""Team digest engine (plan §6.6) + the aggregation shared with the
team-health API (contract §2.9).

Each person gets at most one digest per day: among the digest rules
scheduled for today whose audience includes them, the highest `priority`
wins (ties → lowest id). The digest lists the candidates due today under
each recruiter in scope (contract §6.4), one line per message, split into
parts when it is longer than one WhatsApp message holds.
Nobody is messaged on a day with no candidate due in their scope, and
nobody without a phone number is ever messaged (sender prechecks).
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.whatsapp_layer import clawback_engine as E

logger = logging.getLogger("app_logger")

AUDIENCE_TYPES = ("team_managers", "has_subordinate_managers", "designation_levels", "roles", "users",
                  "admins")
SCOPES = ("direct_team", "full_subtree", "organisation")
DIGEST_FREQUENCIES = ("daily", "weekly", "weekdays")
SECTIONS = ("active", "due", "done", "missed", "at_risk", "ending_soon", "final_pending")
ENDING_SOON_DAYS = 7


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------
@dataclass
class DigestRuleSpec:
    id: Optional[int]
    name: str
    audience_type: str
    audience_value: Dict[str, Any] = field(default_factory=dict)
    scope: str = "full_subtree"
    frequency_type: str = "daily"
    frequency_value: Dict[str, Any] = field(default_factory=dict)
    send_time: str = "09:30"
    template_id: Optional[int] = None
    skip_if_empty: bool = True
    priority: int = 0
    is_active: bool = True


def as_digest_spec(obj) -> DigestRuleSpec:
    g = (lambda k, d=None: obj.get(k, d)) if isinstance(obj, dict) else (lambda k, d=None: getattr(obj, k, d))
    return DigestRuleSpec(
        id=g("id"), name=g("name") or "", audience_type=g("audience_type"),
        audience_value=dict(g("audience_value") or {}), scope=g("scope") or "full_subtree",
        frequency_type=g("frequency_type") or "daily",
        frequency_value=dict(g("frequency_value") or {}), send_time=g("send_time") or "09:30",
        template_id=g("template_id"), skip_if_empty=bool(g("skip_if_empty", True)),
        priority=int(g("priority") or 0), is_active=bool(g("is_active", True)),
    )


def validate_digest_fields(data: Dict[str, Any]) -> List[Dict[str, str]]:
    errs: List[Dict[str, str]] = []
    at, av = data.get("audience_type"), data.get("audience_value") or {}
    if at not in AUDIENCE_TYPES:
        errs.append({"field": "audience_type", "message": f"Must be one of {', '.join(AUDIENCE_TYPES)}"})
    elif at == "designation_levels" and av.get("min_level") is None and av.get("max_level") is None:
        errs.append({"field": "audience_value", "message": "Give min_level and/or max_level"})
    elif at == "roles" and not av.get("role_ids"):
        errs.append({"field": "audience_value", "message": "Give role_ids"})
    elif at == "users" and not av.get("user_ids"):
        errs.append({"field": "audience_value", "message": "Give user_ids"})
    if data.get("scope") not in SCOPES:
        errs.append({"field": "scope", "message": f"Must be one of {', '.join(SCOPES)}"})
    ft, fv = data.get("frequency_type"), data.get("frequency_value") or {}
    if ft not in DIGEST_FREQUENCIES:
        errs.append({"field": "frequency_type", "message": f"Must be one of {', '.join(DIGEST_FREQUENCIES)}"})
    elif ft == "weekly" and str(fv.get("weekday") or "").upper() not in E.WEEKDAYS:
        errs.append({"field": "frequency_value", "message": "weekly needs {\"weekday\": \"MON\"}"})
    elif ft == "weekdays":
        wds = fv.get("weekdays")
        if not isinstance(wds, list) or not wds or any(str(w).upper() not in E.WEEKDAYS for w in wds):
            errs.append({"field": "frequency_value", "message": "weekdays needs {\"weekdays\": [\"MON\", ...]}"})
    if E.parse_hhmm(data.get("send_time")) is None:
        errs.append({"field": "send_time", "message": "Use HH:MM (24h)"})
    sections = data.get("sections")
    if sections is not None and (not isinstance(sections, list)
                                 or any(s not in SECTIONS for s in sections)):
        errs.append({"field": "sections", "message": f"Allowed sections: {', '.join(SECTIONS)}"})
    return errs


def is_digest_due(rule: DigestRuleSpec, d: date) -> bool:
    wd, fv = E.weekday_code(d), rule.frequency_value or {}
    if rule.frequency_type == "daily":
        return True
    if rule.frequency_type == "weekly":
        return wd == str(fv.get("weekday") or "MON").upper()
    if rule.frequency_type == "weekdays":
        return wd in {str(x).upper() for x in (fv.get("weekdays") or [])}
    return False


def digest_period(rule: DigestRuleSpec, today: date) -> Tuple[date, str]:
    """(period start, label). Daily = today; weekly = last 7 days;
    weekdays = since the previous scheduled weekday."""
    if rule.frequency_type == "weekly":
        return today - timedelta(days=6), "last 7 days"
    if rule.frequency_type == "weekdays":
        for k in range(1, 8):
            if is_digest_due(rule, today - timedelta(days=k)):
                start = today - timedelta(days=k - 1)
                return start, ("today" if start == today else f"since {start.strftime('%d %b')}")
    return today, "today"


def audience_request(rule: DigestRuleSpec) -> Dict[str, Any]:
    """Digest audience → org-service request (contract §3.2). `admins` is
    resolved locally from users.role_id → roles.role_type."""
    av = rule.audience_value or {}
    if rule.audience_type in ("team_managers", "has_subordinate_managers", "admins"):
        return {"audience": rule.audience_type}
    if rule.audience_type == "designation_levels":
        return {"audience": "all_managers", "min_level": av.get("min_level"),
                "max_level": av.get("max_level")}
    if rule.audience_type == "roles":
        return {"audience": "all_managers", "role_ids": av.get("role_ids") or []}
    return {"audience": "users", "user_ids": av.get("user_ids") or []}


def assign_recipients(rule_audiences: List[Tuple[DigestRuleSpec, List[Dict[str, Any]]]]
                      ) -> Dict[int, Tuple[DigestRuleSpec, Dict[str, Any]]]:
    """One digest per person per day: the highest-priority rule wins."""
    ordered = sorted(rule_audiences, key=lambda ra: (-ra[0].priority, ra[0].id or 0))
    out: Dict[int, Tuple[DigestRuleSpec, Dict[str, Any]]] = {}
    for rule, users in ordered:
        for u in users:
            uid = int(u.get("user_id") or 0)
            if uid and uid not in out:
                out[uid] = (rule, u)
    return out


def scope_user_ids(rule_scope: str, user_record: Dict[str, Any]) -> Set[int]:
    key = "direct_team_user_ids" if rule_scope == "direct_team" else "subordinate_user_ids"
    return {int(x) for x in (user_record.get(key) or [])}


def stage_label_for(item: Dict[str, Any], rules: List[E.RuleSpec], today: date) -> str:
    anchor, day = E.anchor_and_day(item, today)
    if anchor is None:
        return "Not started"
    inside = [r for r in rules if r.is_active and r.anchor == anchor and E.in_window(r, day)]
    if inside:
        inside.sort(key=lambda r: (-r.priority, r.id or 0))
        return inside[0].stage_label or inside[0].name
    return {"offer_accepted": "Pre-joining", "doj": "In cooling",
            "cooling_end": "Cooling ended"}[anchor]


def aggregate(scope: Optional[Set[int]], items: List[Dict[str, Any]],
              checks: List[Dict[str, Any]], rules: List[E.RuleSpec],
              today: date, period_start: date) -> Dict[str, Any]:
    """Totals for a recruiter scope (None = everyone). `checks` are dicts with
    id, recruiter_user_id, due_date, status, outcome."""
    def in_scope(uid) -> bool:
        return scope is None or (uid is not None and int(uid) in scope)

    scoped_items = [i for i in items if in_scope(E.recruiter_for(i, True))]
    ending_soon, final_pending = [], []
    for i in scoped_items:
        comp = E.completion_date_of(i)
        if not comp or not i.get("joined_at"):
            continue
        if today >= comp:
            final_pending.append(i)
        elif comp <= today + timedelta(days=ENDING_SOON_DAYS):
            ending_soon.append(i)

    period = [c for c in checks if in_scope(c.get("recruiter_user_id"))
              and period_start <= c["due_date"] <= today and c.get("status") != "cancelled"]
    status_n = Counter(c.get("status") for c in period)
    outcome_n = Counter(c.get("outcome") for c in period if c.get("outcome"))

    per_rec: Dict[int, Dict[str, int]] = defaultdict(lambda: {"due": 0, "done": 0, "missed": 0})
    for c in period:
        rid = c.get("recruiter_user_id")
        if rid is None:
            continue
        per_rec[int(rid)]["due"] += 1
        if c.get("status") in ("done", "missed"):
            per_rec[int(rid)][c["status"]] += 1

    by_stage = Counter(stage_label_for(i, rules, today) for i in scoped_items)
    top_missed = [rid for rid, v in sorted(per_rec.items(), key=lambda kv: -kv[1]["missed"])
                  if v["missed"] > 0][:3]
    return {
        "totals": {
            "active": len(scoped_items),
            "due": len(period),
            "done": status_n.get("done", 0),
            "missed": status_n.get("missed", 0),
            "at_risk": outcome_n.get("at_risk", 0),
            "not_reachable": outcome_n.get("not_reachable", 0),
            "dropped": outcome_n.get("dropped", 0),
            "ending_soon": len(ending_soon),
            "final_pending": len(final_pending),
        },
        "by_stage": [{"stage_label": k, "count": v} for k, v in sorted(by_stage.items())],
        "recruiters": dict(per_rec),
        "top_missed_recruiter_ids": top_missed,
        "at_risk_check_ids": [c["id"] for c in period
                              if c.get("outcome") in ("at_risk", "not_reachable", "dropped")],
        "ending_soon_items": ending_soon,
    }


def is_empty(totals: Dict[str, int]) -> bool:
    return not any(totals.get(k) for k in ("active", "due", "done", "missed", "final_pending"))


def team_list_parts(entries: List[Dict[str, Any]], recruiter_names: Dict[int, str],
                    base_url: Optional[str]) -> List[Dict[str, Any]]:
    """Today's due candidates grouped by recruiter, split into messages:
    "Asha R (2): 1) Ravi K – Acme – joined 12 Oct, day 14 of 90 – <link> · 2) …
    | Vikram D (1): 1) …". One dict per part: `part_label`, `candidate_list`,
    `candidates`."""
    from app.whatsapp_layer import variables as V
    by_rec: Dict[Optional[int], List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_rec[e.get("recruiter_user_id")].append(e)

    def name_of(rid):
        return V.sanitize_text(recruiter_names.get(rid) if rid else "No recruiter")

    rows = []
    for rid in sorted(by_rec, key=lambda r: (name_of(r).lower(), r or 0)):
        es, name = by_rec[rid], name_of(rid)
        for n, e in enumerate(es, start=1):
            text = E.list_entry_text(n, e, base_url, with_action=False)
            rows.append((f"{name} ({len(es)})", f"{name} (contd.)", text, e, rid))
    parts = V.split_list([(h, c, t) for h, c, t, _e, _r in rows])
    out, i = [], 0
    for k, (value, shown) in enumerate(parts, start=1):
        chunk = rows[i:i + shown]
        out.append({
            "part_label": V.part_label(len(entries), k, len(parts)),
            "candidate_list": value,
            "candidates": [{"recruiter_user_id": r, "entry": t,
                            "candidate_job_id": e["item"].get("candidate_job_id"),
                            "health_check_id": e["check"].id if e.get("check") is not None else None}
                           for _h, _c, t, e, r in chunk],
        })
        i += shown
    return out


def due_today(db: Session, organization_id: int, items: List[Dict[str, Any]],
              rules: List[E.RuleSpec], today: date,
              fallback_to_created_by: bool) -> List[Dict[str, Any]]:
    """Candidates a rule makes due today (the recruiters' list for today).
    Their health checks are created now if the rule makes one, so the
    summary can link to them even when it goes out before the recruiters'
    reminders."""
    wd = E.weekday_code(today)
    out = []
    for item in items:
        anchor, day = E.anchor_and_day(item, today)
        if anchor is None or E.cooling_days(item) <= 0:
            continue
        rule = E.match_rule(rules, anchor, day, wd)
        if rule is None:
            continue
        rid = E.recruiter_for(item, fallback_to_created_by)
        check = None
        if rule.creates_health_check:
            check = E.ensure_health_check(db, organization_id, item, rule, anchor, day, today, rid)
            if check is not None and check.status == "cancelled":
                continue
        out.append({"item": item, "rule": rule, "anchor": anchor, "day": day,
                    "recruiter_user_id": rid, "check": check})
    return out


def digest_params(totals: Dict[str, int], recipient_name: Optional[str], scope_label: str,
                  period_label: str, app_link: str) -> Dict[str, str]:
    """Count variables (kept for templates written before the list)."""
    return {
        "recipient_name": recipient_name or "-",
        "scope_label": scope_label,
        "period_label": period_label,
        "active_count": str(totals["active"]),
        "due_count": str(totals["due"]),
        "done_count": str(totals["done"]),
        "missed_count": str(totals["missed"]),
        # "At risk" in the digest covers at_risk + not_reachable + dropped.
        "at_risk_count": str(totals["at_risk"] + totals["not_reachable"] + totals["dropped"]),
        "ending_soon_count": str(totals["ending_soon"]),
        "final_pending_count": str(totals["final_pending"]),
        "app_link": app_link,
    }


def scope_label(n_recruiters: int) -> str:
    return f"Your team ({n_recruiters} recruiter{'s' if n_recruiters != 1 else ''})"


# ---------------------------------------------------------------------------
# DB glue
# ---------------------------------------------------------------------------
def check_dicts(db: Session, organization_id: int, since: date) -> List[Dict[str, Any]]:
    from app.whatsapp_layer.models import ClawbackHealthCheck as H
    rows = (db.query(H.id, H.recruiter_user_id, H.due_date, H.status, H.outcome)
            .filter(H.organization_id == organization_id, H.due_date >= since).all())
    return [{"id": r[0], "recruiter_user_id": r[1], "due_date": E.parse_date(r[2]),
             "status": r[3], "outcome": r[4]} for r in rows]


def active_digest_rules(db: Session, organization_id: int) -> List:
    from app.whatsapp_layer.models import ClawbackDigestRule as D
    return (db.query(D).filter(D.organization_id == organization_id, D.is_active == 1,
                               D.deleted_at.is_(None))
            .order_by(D.priority.desc(), D.id.asc()).all())


def run_org_digests(db: Session, organization_id: int, now_utc: Optional[datetime] = None,
                    dispatch: Optional[Callable[[int], None]] = None,
                    items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
    from app.whatsapp_layer import accounts as acct_svc, clients, people, sender
    from app.whatsapp_layer import templates_service as ts
    from app.whatsapp_layer import variables as V
    from app.whatsapp_layer.models import WhatsAppMessageLog as L

    stats = {"queued": 0, "skipped": 0, "empty": 0}
    st = E.get_settings(db, organization_id)
    if not st or not st.enabled:
        return stats
    now_local = E.local_now(st.timezone, now_utc)
    today = now_local.date()
    if not E.is_working_day(today, st.working_days) or \
            E.in_quiet_hours(now_local.time(), st.quiet_hours_start, st.quiet_hours_end):
        return stats
    scheduled = [as_digest_spec(r) for r in active_digest_rules(db, organization_id)]
    scheduled = [r for r in scheduled if is_digest_due(r, today)]
    if not any(E.time_reached(now_local, r.send_time) for r in scheduled):
        return stats

    rule_audiences = []
    for rule in scheduled:
        try:
            req = audience_request(rule)
            audience = req.pop("audience")
            users = (people.admin_users(db, organization_id) if audience == "admins"
                     else clients.fetch_subordinates(organization_id, audience, **req))
            rule_audiences.append((rule, users))
        except Exception as exc:
            db.rollback()
            logger.error("Digest rule %s audience lookup failed: %s", rule.id, exc)
    assignment = assign_recipients(rule_audiences)
    ready = {uid: ru for uid, ru in assignment.items() if E.time_reached(now_local, ru[0].send_time)}
    if not ready:
        return stats

    # Already sent today? (one digest per person per org per day; platform
    # super admins get one from every org)
    keys = {uid: E.idempotency_key("digest", 0, organization_id, uid, today) for uid in ready}
    done_keys = {k for (k,) in db.query(L.idempotency_key)
                 .filter(L.idempotency_key.in_(list(keys.values()))).all()}
    ready = {uid: ru for uid, ru in ready.items() if keys[uid] not in done_keys}
    if not ready:
        return stats

    if items is None:
        items = clients.fetch_active_clawbacks(organization_id).get("items") or []
    rules = [E.as_rule_spec(r) for r in E.active_rules(db, organization_id)]
    checks = check_dicts(db, organization_id, today - timedelta(days=7))
    account = acct_svc.get_active_account(db, organization_id)
    recipients = people.get_users(db, list(ready))
    to_dispatch: List[int] = []

    due = due_today(db, organization_id, items, rules, today, bool(st.fallback_to_created_by))
    db.commit()
    rec_names = people.get_users(db, list({e["recruiter_user_id"] for e in due
                                            if e["recruiter_user_id"]}))
    rec_names = {uid: u.get("name") for uid, u in rec_names.items()}

    for uid, (rule, record) in ready.items():
        org_wide = rule.scope == "organisation" or rule.audience_type == "admins"
        scope = None if org_wide else scope_user_ids(rule.scope, record) | {uid}
        start, plabel = digest_period(rule, today)
        agg = aggregate(scope, items, checks, rules, today, start)
        totals = agg["totals"]
        mine = [e for e in due if scope is None
                or (e["recruiter_user_id"] is not None and int(e["recruiter_user_id"]) in scope)]
        # No candidate to follow up today: no message, whatever the rule says.
        if not mine:
            stats["empty"] += 1
            continue
        user = recipients.get(uid) or {}
        template = (sender.rule_template(db, rule.template_id) if rule.template_id
                    else sender.approved_template_for_purpose(db, organization_id, "team_digest"))
        n_rec = (len({i_rec for i_rec in (E.recruiter_for(i, True) for i in items) if i_rec})
                 if scope is None else len(scope - {uid}))
        base_params = digest_params(totals, user.get("name") or record.get("name"),
                                    scope_label(n_rec), plabel,
                                    V.app_link(st.app_base_url, "team_digest"))
        base_params.update({
            "due_count": str(len(mine)),
            "recruiter_count": str(len({e["recruiter_user_id"] for e in mine})),
            "recruiter_summary": V.names_list(sorted(
                f"{rec_names.get(rid) or 'Unassigned'} ({n})" for rid, n in
                Counter(e["recruiter_user_id"] for e in mine).items())),
        })
        parts = team_list_parts(mine, rec_names, st.app_base_url)
        if template is not None and parts and \
                "candidate_list" not in ts.extract_keys(template.body_text_named):
            parts = [{**parts[0], "part_label": V.part_label(len(mine), 1, 1),
                      "candidates": [c for p in parts for c in p["candidates"]]}]
        snapshot = {"period_start": start.isoformat(), "period_end": today.isoformat(),
                    "scope": rule.scope, "scope_user_count": n_rec, "totals": totals,
                    "by_stage": agg["by_stage"],
                    "top_missed_recruiter_ids": agg["top_missed_recruiter_ids"]}
        for n, part in enumerate(parts, start=1):
            key = keys[uid] if n == 1 else E.idempotency_key("digest", n, organization_id, uid, today)
            log, created = sender.create_log(
                db, organization_id=organization_id, kind="digest", idempotency_key=key,
                account_id=account.id if account else None,
                provider=account.provider if account else None,
                digest_rule_id=rule.id, rule_name=rule.name,
                template_id=template.id if template else rule.template_id,
                template_name=template.name if template else None,
                recipient_user_id=uid, recipient_name=user.get("name") or record.get("name"),
                recipient_type="manager", phone_e164=user.get("phone_e164"),
                scheduled_for=E.local_to_utc_naive(
                    today, E.parse_hhmm(rule.send_time, time(9, 30)), st.timezone),
                health_check_ids=[c["health_check_id"] for c in part["candidates"]
                                  if c["health_check_id"]] or None,
                params={**base_params, **part}, digest_snapshot=snapshot)
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


def run_digests(db: Session, now_utc: Optional[datetime] = None,
                dispatch: Optional[Callable[[int], None]] = None) -> Dict[str, int]:
    """evaluate_clawback_digests: every enabled org."""
    from app.whatsapp_layer import eod_report
    totals = {"orgs": 0, "queued": 0, "skipped": 0, "empty": 0}
    for org_id in E.enabled_org_ids(db):
        try:
            stats = run_org_digests(db, org_id, now_utc, dispatch)
            eod = eod_report.run_org_eod(db, org_id, now_utc, dispatch)
            stats = {k: stats[k] + eod[k] for k in stats}
            totals["orgs"] += 1
            for k in ("queued", "skipped", "empty"):
                totals[k] += stats[k]
        except Exception as exc:
            db.rollback()
            logger.error("Clawback digests failed for org %s: %s", org_id, exc, exc_info=True)
    return totals
