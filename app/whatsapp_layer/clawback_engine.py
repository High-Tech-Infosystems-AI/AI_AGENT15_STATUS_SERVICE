"""Clawback reminder engine (plan §6, contract §0/§2.6/§2.8/§2.9).

Pure functions first (day numbers, "is due", overlap, quiet hours,
idempotency key, batching) — no DB, unit-tested at every window boundary.
DB glue follows: settings, health checks, the 15-minute recruiter run,
missed-check marking, clawback_state_changed handling, send-now, simulate.

Day numbers (contract §0):
  - doj / offer_accepted: day = (today_local − anchor_date) + 1  → anchor = Day 1
  - cooling_end:          day = today_local − completion_date    → completion = Day 0
  - doj rules never fire on/after the completion date; only cooling_end rules do.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
ANCHORS = ("offer_accepted", "doj", "cooling_end")
FREQUENCY_TYPES = ("every_n_days", "specific_days", "weekly", "weekdays", "once")
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_WORKING_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT"]
SIMULATE_MAX_DAYS = 400


# ---------------------------------------------------------------------------
# Pure: parsing
# ---------------------------------------------------------------------------
def parse_date(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_hhmm(value, default: Optional[time] = None) -> Optional[time]:
    if value is None or value == "":
        return default
    if isinstance(value, time):
        return value
    try:
        hh, mm = str(value).strip().split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return default


def zone(name: Optional[str]):
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r; falling back to %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_now(tz_name: Optional[str], now_utc: Optional[datetime] = None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(zone(tz_name))


def local_to_utc_naive(d: date, t: time, tz_name: Optional[str]) -> datetime:
    return (datetime.combine(d, t, tzinfo=zone(tz_name))
            .astimezone(timezone.utc).replace(tzinfo=None))


def weekday_code(d: date) -> str:
    return WEEKDAYS[d.weekday()]


# ---------------------------------------------------------------------------
# Pure: rules
# ---------------------------------------------------------------------------
@dataclass
class RuleSpec:
    id: Optional[int]
    name: str
    anchor: str
    start_day: int
    end_day: Optional[int]
    frequency_type: str
    frequency_value: Dict[str, Any] = field(default_factory=dict)
    send_time: str = "10:00"
    priority: int = 0
    is_active: bool = True
    template_id: Optional[int] = None
    creates_health_check: bool = True
    stage_label: Optional[str] = None


def as_rule_spec(obj) -> RuleSpec:
    """Model row or dict → RuleSpec."""
    g = (lambda k, d=None: obj.get(k, d)) if isinstance(obj, dict) else (lambda k, d=None: getattr(obj, k, d))
    return RuleSpec(
        id=g("id"), name=g("name") or "", anchor=g("anchor"),
        start_day=int(g("start_day") or 0),
        end_day=(int(g("end_day")) if g("end_day") is not None else None),
        frequency_type=g("frequency_type"),
        frequency_value=dict(g("frequency_value") or {}),
        send_time=g("send_time") or "10:00",
        priority=int(g("priority") or 0),
        is_active=bool(g("is_active", True)),
        template_id=g("template_id"),
        creates_health_check=bool(g("creates_health_check", True)),
        stage_label=g("stage_label"),
    )


def in_window(rule: RuleSpec, day: int) -> bool:
    return rule.start_day <= day and (rule.end_day is None or day <= rule.end_day)


def is_rule_due(rule: RuleSpec, day: int, weekday: str) -> bool:
    """Plan §6.2. `day` is already computed for the rule's anchor."""
    if not in_window(rule, day):
        return False
    ft, fv = rule.frequency_type, rule.frequency_value or {}
    if ft == "every_n_days":
        n = max(1, int(fv.get("n") or 1))
        return (day - rule.start_day) % n == 0
    if ft == "specific_days":
        return day in {int(x) for x in (fv.get("days") or [])}
    if ft == "weekly":
        return (day - rule.start_day) % 7 == 0
    if ft == "weekdays":
        return weekday in {str(x).upper() for x in (fv.get("weekdays") or [])}
    if ft == "once":
        return day == rule.start_day
    return False


def match_rule(rules: Iterable[RuleSpec], anchor: str, day: int,
               weekday: str) -> Optional[RuleSpec]:
    """At most one rule per clawback per day: the highest priority due rule
    (ties → lowest id)."""
    due = [r for r in rules if r.is_active and r.anchor == anchor and is_rule_due(r, day, weekday)]
    if not due:
        return None
    due.sort(key=lambda r: (-r.priority, r.id or 0))
    return due[0]


def windows_overlap(a: RuleSpec, b: RuleSpec) -> bool:
    if a.anchor != b.anchor:
        return False
    a_end = a.end_day if a.end_day is not None else float("inf")
    b_end = b.end_day if b.end_day is not None else float("inf")
    return a.start_day <= b_end and b.start_day <= a_end


def find_overlap(candidate: RuleSpec, others: Iterable[RuleSpec]) -> Optional[RuleSpec]:
    """First active rule (other than `candidate`) sharing a day on the same
    anchor. Inactive candidates never conflict."""
    if not candidate.is_active:
        return None
    for o in others:
        if o.is_active and (candidate.id is None or o.id != candidate.id) and windows_overlap(candidate, o):
            return o
    return None


def validate_rule_fields(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Field-level checks for a recruiter rule (422 detail list)."""
    errs: List[Dict[str, str]] = []
    anchor = data.get("anchor")
    if anchor not in ANCHORS:
        errs.append({"field": "anchor", "message": f"Must be one of {', '.join(ANCHORS)}"})
    start, end = data.get("start_day"), data.get("end_day")
    min_start = 0 if anchor == "cooling_end" else 1
    if start is None or int(start) < min_start:
        errs.append({"field": "start_day", "message": f"Must be ≥ {min_start}"})
    if end is not None and start is not None and int(end) < int(start):
        errs.append({"field": "end_day", "message": "Must be ≥ start_day (or empty for open-ended)"})
    ft, fv = data.get("frequency_type"), data.get("frequency_value") or {}
    if ft not in FREQUENCY_TYPES:
        errs.append({"field": "frequency_type", "message": f"Must be one of {', '.join(FREQUENCY_TYPES)}"})
    elif ft == "every_n_days":
        try:
            if int(fv.get("n") or 0) < 1:
                raise ValueError
        except (TypeError, ValueError):
            errs.append({"field": "frequency_value", "message": "every_n_days needs {\"n\": ≥1}"})
    elif ft == "specific_days":
        days = fv.get("days")
        if not isinstance(days, list) or not days or not all(isinstance(d, int) for d in days):
            errs.append({"field": "frequency_value", "message": "specific_days needs {\"days\": [int, ...]}"})
    elif ft == "weekdays":
        wds = fv.get("weekdays")
        if not isinstance(wds, list) or not wds or any(str(w).upper() not in WEEKDAYS for w in wds):
            errs.append({"field": "frequency_value", "message": "weekdays needs {\"weekdays\": [\"MON\", ...]}"})
    if parse_hhmm(data.get("send_time")) is None:
        errs.append({"field": "send_time", "message": "Use HH:MM (24h)"})
    if data.get("recipient_type", "recruiter") != "recruiter":
        errs.append({"field": "recipient_type", "message": "Only 'recruiter' is supported"})
    return errs


# ---------------------------------------------------------------------------
# Pure: clawback item → anchor + day
# ---------------------------------------------------------------------------
def cooling_days(item: Dict[str, Any]) -> int:
    try:
        return max(0, int(float(item.get("cooling_period_days") or 0)))
    except (TypeError, ValueError):
        return 0


def completion_date_of(item: Dict[str, Any]) -> Optional[date]:
    explicit = parse_date(item.get("clawback_completion_date"))
    if explicit:
        return explicit
    joined = parse_date(item.get("joined_at"))
    days = cooling_days(item)
    return joined + timedelta(days=days) if joined and days > 0 else None


def day_number(anchor: str, today: date, anchor_date: date) -> int:
    if anchor == "cooling_end":
        return (today - anchor_date).days
    return (today - anchor_date).days + 1


def anchor_and_day(item: Dict[str, Any], today: date) -> Tuple[Optional[str], Optional[int]]:
    """Which anchor applies to a clawback on `today`, and its day number.

    pre_joining → offer_accepted; joined before completion → doj;
    on/after completion → cooling_end. Closed clawbacks → (None, None)."""
    if item.get("cooling_period_closed"):
        return None, None
    joined = parse_date(item.get("joined_at"))
    if item.get("status") == "pre_joining" or not joined:
        oa = parse_date(item.get("offer_accepted_at"))
        if not oa or today < oa:
            return None, None
        return "offer_accepted", day_number("offer_accepted", today, oa)
    if today < joined:
        return None, None
    comp = completion_date_of(item)
    if comp and today >= comp:
        return "cooling_end", day_number("cooling_end", today, comp)
    return "doj", day_number("doj", today, joined)


def recruiter_for(item: Dict[str, Any], fallback_to_created_by: bool) -> Optional[int]:
    if fallback_to_created_by:
        rid = item.get("recruiter_user_id") or item.get("assigned_to") or item.get("created_by")
    else:
        rid = item.get("assigned_to")
    return int(rid) if rid else None


# ---------------------------------------------------------------------------
# Pure: time windows
# ---------------------------------------------------------------------------
def in_quiet_hours(now_t: time, start, end) -> bool:
    """Quiet window [start, end) in local time; may wrap midnight."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s is None or e is None or s == e:
        return False
    if s < e:
        return s <= now_t < e
    return now_t >= s or now_t < e


def is_working_day(d: date, working_days: Optional[List[str]]) -> bool:
    days = working_days if working_days else DEFAULT_WORKING_DAYS
    return weekday_code(d) in {str(x).upper() for x in days}


def time_reached(now_local: datetime, hhmm) -> bool:
    t = parse_hhmm(hhmm, time(0, 0))
    return now_local.time().replace(second=0, microsecond=0) >= t


# ---------------------------------------------------------------------------
# Pure: idempotency + batching
# ---------------------------------------------------------------------------
def idempotency_key(kind: str, rule_id: Optional[int], subject_id: Optional[int],
                    recipient_user_id: Optional[int], local_date: date) -> str:
    """sha1(kind, rule/digest id, subject id, recipient, local date) — stable
    across runs, processes and restarts."""
    raw = f"{kind}|{int(rule_id or 0)}|{int(subject_id or 0)}|{int(recipient_user_id or 0)}|{local_date.isoformat()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class PlannedMessage:
    kind: str                       # reminder | batch_reminder | final_validation
    recruiter_user_id: Optional[int]
    entries: List[Dict[str, Any]]


def plan_recruiter_messages(entries: List[Dict[str, Any]],
                            threshold: Optional[int],
                            combine: bool = True) -> List[PlannedMessage]:
    """Group a run's due entries per recruiter (plan §6.3 step 3, contract §6.1).

    final_validation entries are always individual. For reminders: up to
    `threshold` per recruiter → one message each; more → one batch_reminder
    (default threshold 1: 2+ due → one combined message).
    threshold None/≤0, or `combine` False (system:recruiter_summary off),
    disables batching."""
    out: List[PlannedMessage] = []
    by_rec: Dict[Optional[int], List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        if e.get("kind") == "final_validation":
            out.append(PlannedMessage("final_validation", e.get("recruiter_user_id"), [e]))
        else:
            by_rec[e.get("recruiter_user_id")].append(e)
    for rec, es in by_rec.items():
        if combine and threshold and threshold > 0 and len(es) > threshold:
            out.append(PlannedMessage("batch_reminder", rec, es))
        else:
            out.extend(PlannedMessage("reminder", rec, [e]) for e in es)
    return out


# ---------------------------------------------------------------------------
# Pure: schedule projection (simulate / next reminder)
# ---------------------------------------------------------------------------
def project_schedule(rules: List[RuleSpec], item: Dict[str, Any], start: date,
                     days: int) -> List[Dict[str, Any]]:
    """Every (date, anchor, day, rule) the rules would fire for `item` from
    `start` for `days` days. Pre-joining items stay on the offer_accepted
    anchor (the joining date isn't known yet)."""
    out = []
    for i in range(max(0, min(days, SIMULATE_MAX_DAYS))):
        d = start + timedelta(days=i)
        anchor, day = anchor_and_day(item, d)
        if anchor is None:
            continue
        rule = match_rule(rules, anchor, day, weekday_code(d))
        if rule:
            out.append({"date": d, "anchor": anchor, "day_number": day, "rule": rule})
    return out


def next_reminder_date(rules: List[RuleSpec], item: Dict[str, Any], start: date,
                       horizon: int = 200) -> Optional[date]:
    events = project_schedule(rules, item, start, horizon)
    return events[0]["date"] if events else None


# ---------------------------------------------------------------------------
# Pure: message params
# ---------------------------------------------------------------------------
def reminder_params(item: Dict[str, Any], rule: RuleSpec, day: int,
                    recruiter_name: Optional[str], app_link: str,
                    today: date) -> Dict[str, Any]:
    comp = completion_date_of(item)
    cp = cooling_days(item)
    joined = parse_date(item.get("joined_at"))
    if joined and cp > 0:
        elapsed = (today - joined).days
        progress = max(0, min(100, round(elapsed / cp * 100)))
    else:
        progress = 0
    return {
        "recruiter_name": recruiter_name or "-",
        "candidate_name": item.get("candidate_name") or "-",
        "job_title": item.get("job_title") or "-",
        "company_name": item.get("company_name") or "-",
        "offer_accepted_date": str(parse_date(item.get("offer_accepted_at")) or "-"),
        "joining_date": str(joined or "-"),
        "clawback_completion_date": str(comp or "-"),
        "day_number": str(day),
        "cooling_period_days": str(cp) if cp > 0 else "-",
        "stage_label": rule.stage_label or rule.name or "-",
        "days_pending": str(max(0, (comp - today).days)) if comp else "-",
        "progress_percent": str(progress),
        "app_link": app_link,
    }


def candidate_list_params(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`candidate_count` / `candidate_list` for a batch_reminder (contract
    §6.1), plus `candidates`: the full list (the message may show "+k more")."""
    from app.whatsapp_layer import variables as V
    lines, full = [], []
    for n, e in enumerate(entries, start=1):
        item, anchor = e["item"], e.get("anchor")
        cp = cooling_days(item)
        oa = parse_date(item.get("offer_accepted_at"))
        line = V.candidate_list_entry(
            n, candidate_name=item.get("candidate_name"), job_title=item.get("job_title"),
            company_name=item.get("company_name"), pre_joining=anchor == "offer_accepted",
            day_number=e.get("day"), cooling_period_days=str(cp) if cp > 0 else "-",
            offer_accepted_date=oa)
        lines.append(line)
        chk = e.get("check")
        full.append({"candidate_job_id": item.get("candidate_job_id"),
                     "health_check_id": chk.id if chk is not None else None,
                     "candidate_name": item.get("candidate_name"),
                     "job_title": item.get("job_title"), "company_name": item.get("company_name"),
                     "anchor": anchor, "day_number": e.get("day"), "entry": line})
    value, _shown = V.format_candidate_list(lines)
    return {"candidate_count": str(len(entries)), "candidate_list": value, "candidates": full}


# ===========================================================================
# DB glue
# ===========================================================================
def settings_dict(st) -> Dict[str, Any]:
    return {
        "organization_id": st.organization_id,
        "enabled": bool(st.enabled),
        "timezone": st.timezone or DEFAULT_TIMEZONE,
        "quiet_hours_start": st.quiet_hours_start,
        "quiet_hours_end": st.quiet_hours_end,
        "working_days": list(st.working_days or DEFAULT_WORKING_DAYS),
        "recruiter_batch_threshold": st.recruiter_batch_threshold,
        "missed_check_cutoff_time": st.missed_check_cutoff_time,
        "fallback_to_created_by": bool(st.fallback_to_created_by),
        "digest_skip_if_empty": bool(st.digest_skip_if_empty),
        "app_base_url": st.app_base_url,
    }


def get_settings(db: Session, organization_id: int, create: bool = True):
    """The org's settings row, created with defaults on first read."""
    from app.whatsapp_layer.models import ClawbackNotificationSettings as S
    st = db.query(S).filter(S.organization_id == organization_id).first()
    if st or not create:
        return st
    from app.core import settings as app_settings
    st = S(organization_id=organization_id, enabled=0, timezone=DEFAULT_TIMEZONE,
           quiet_hours_start="21:00", quiet_hours_end="08:00",
           working_days=list(DEFAULT_WORKING_DAYS), recruiter_batch_threshold=1,
           missed_check_cutoff_time="23:00", fallback_to_created_by=1,
           digest_skip_if_empty=1,
           app_base_url=(getattr(app_settings, "WEB_PUSH_FRONTEND_BASE_URL", "") or None))
    db.add(st)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # concurrent first read
    return db.query(S).filter(S.organization_id == organization_id).first()


def active_rules(db: Session, organization_id: int) -> List:
    from app.whatsapp_layer.models import ClawbackNotificationRule as R
    return (db.query(R).filter(R.organization_id == organization_id, R.is_active == 1,
                               R.deleted_at.is_(None))
            .order_by(R.anchor, R.start_day).all())


def ensure_health_check(db: Session, organization_id: int, item: Dict[str, Any],
                        rule: RuleSpec, anchor: str, day: int, due: date,
                        recruiter_user_id: Optional[int]):
    """Insert-or-ignore on UNIQUE(candidate_job_id, due_date)."""
    from app.whatsapp_layer.models import ClawbackHealthCheck as H
    cj = int(item["candidate_job_id"])
    existing = db.query(H).filter(H.candidate_job_id == cj, H.due_date == due).first()
    if existing:
        return existing
    row = H(organization_id=organization_id, candidate_job_id=cj,
            candidate_job_status_id=item.get("candidate_job_status_id"),
            candidate_id=item.get("candidate_id"), candidate_name=item.get("candidate_name"),
            job_id=item.get("job_id"), job_title=item.get("job_title"),
            company_name=item.get("company_name"), recruiter_user_id=recruiter_user_id,
            rule_id=rule.id, stage_label=rule.stage_label or rule.name, anchor=anchor,
            day_number=day, due_date=due, status="pending")
    try:
        with db.begin_nested():
            db.add(row)
        return row
    except IntegrityError:
        return db.query(H).filter(H.candidate_job_id == cj, H.due_date == due).first()


def _rule_specs(rows) -> List[RuleSpec]:
    return [as_rule_spec(r) for r in rows]


def evaluate_org_reminders(db: Session, organization_id: int, items: List[Dict[str, Any]],
                           now_utc: Optional[datetime] = None,
                           dispatch: Optional[Callable[[int], None]] = None) -> Dict[str, int]:
    """One recruiter-reminder pass for one org (plan §6.3)."""
    from app.whatsapp_layer import accounts as acct_svc, people, sender
    from app.whatsapp_layer import variables as V
    from app.whatsapp_layer.models import ClawbackHealthCheck as H, WhatsAppMessageLog as L

    stats = {"checks": 0, "queued": 0, "skipped": 0}
    st = get_settings(db, organization_id)
    if not st or not st.enabled:
        return stats
    rules = _rule_specs(active_rules(db, organization_id))
    if not rules:
        return stats
    now_local = local_now(st.timezone, now_utc)
    today, wd = now_local.date(), weekday_code(now_local.date())
    can_send = (is_working_day(today, st.working_days)
                and not in_quiet_hours(now_local.time(), st.quiet_hours_start, st.quiet_hours_end))

    entries: List[Dict[str, Any]] = []
    no_cooling: List[Dict[str, Any]] = []
    for item in items:
        try:
            anchor, day = anchor_and_day(item, today)
            if anchor is None:
                continue
            rule = match_rule(rules, anchor, day, wd)
            if not rule or not time_reached(now_local, rule.send_time):
                continue
            rid = recruiter_for(item, bool(st.fallback_to_created_by))
            if cooling_days(item) <= 0:
                no_cooling.append({"item": item, "rule": rule, "anchor": anchor, "day": day,
                                   "recruiter_user_id": rid})
                continue
            check = None
            if rule.creates_health_check:
                check = ensure_health_check(db, organization_id, item, rule, anchor, day, today, rid)
                stats["checks"] += 1
            else:
                check = (db.query(H).filter(H.candidate_job_id == int(item["candidate_job_id"]),
                                            H.due_date == today).first())
            if check is not None and check.status == "cancelled":
                continue
            entries.append({"item": item, "rule": rule, "anchor": anchor, "day": day,
                            "recruiter_user_id": rid, "check": check,
                            "kind": "final_validation" if anchor == "cooling_end" else "reminder"})
        except Exception as exc:
            logger.error("Clawback reminder evaluation failed for candidate_job %s: %s",
                         item.get("candidate_job_id"), exc, exc_info=True)
    db.commit()
    if not can_send:
        return stats

    account = acct_svc.get_active_account(db, organization_id)
    recipients = people.get_users(db, [e["recruiter_user_id"] for e in entries + no_cooling])
    base = st.app_base_url

    # Jobs with no cooling period: logged (skipped) on the days a rule fires.
    for e in no_cooling:
        if not e["recruiter_user_id"]:
            continue
        key = idempotency_key("reminder", e["rule"].id, e["item"]["candidate_job_id"],
                              e["recruiter_user_id"], today)
        log, created = sender.create_log(db, **_log_fields(
            organization_id, "reminder", key, e["item"], e["rule"], e["day"],
            e["recruiter_user_id"], recipients, account, None, now_utc))
        if created:
            sender.mark_skipped(log, "no_cooling_period")
            stats["skipped"] += 1
    db.commit()

    # Drop entries already handled today (re-runs), and whole recruiters
    # who already got today's batch.
    keys = {id(e): idempotency_key(e["kind"], e["rule"].id, e["item"]["candidate_job_id"],
                                   e["recruiter_user_id"], today) for e in entries}
    batch_keys = {rec: idempotency_key("batch_reminder", 0, 0, rec, today)
                  for rec in {e["recruiter_user_id"] for e in entries}}
    existing = {k for (k,) in db.query(L.idempotency_key).filter(
        L.idempotency_key.in_(list(keys.values()) + list(batch_keys.values()))).all()}
    fresh = [e for e in entries if keys[id(e)] not in existing
             and not (e["kind"] == "reminder" and batch_keys[e["recruiter_user_id"]] in existing)]

    from app.whatsapp_layer import events as EV
    combine = EV.system_enabled(st, EV.RECRUITER_SUMMARY)
    to_dispatch: List[int] = []
    for msg in plan_recruiter_messages(fresh, st.recruiter_batch_threshold, combine):
        rid = msg.recruiter_user_id
        if not rid:
            logger.warning("Clawback reminder for candidate_job %s has no recruiter; not sent",
                           msg.entries[0]["item"].get("candidate_job_id"))
            continue
        user = recipients.get(rid) or {}
        if msg.kind == "batch_reminder":
            template = sender.approved_template_for_purpose(db, organization_id, "recruiter_batch")
            overdue = (db.query(H).filter(H.organization_id == organization_id,
                                          H.recruiter_user_id == rid, H.status == "pending",
                                          H.due_date < today).count())
            params = {"recruiter_name": user.get("name") or "-",
                      "due_count": str(len(msg.entries)), "overdue_count": str(overdue),
                      **candidate_list_params(msg.entries),
                      "app_link": V.app_link(base, "recruiter_batch")}
            fields = _log_fields(organization_id, "batch_reminder",
                                 batch_keys[rid], None, None, None, rid, recipients,
                                 account, template, now_utc)
            fields["health_check_ids"] = [e["check"].id for e in msg.entries if e["check"] is not None]
            fields["scheduled_for"] = local_to_utc_naive(
                today, parse_hhmm(msg.entries[0]["rule"].send_time, time(10, 0)), st.timezone)
        else:
            e = msg.entries[0]
            rule, item = e["rule"], e["item"]
            template = sender.rule_template(db, rule.template_id)
            purpose = "final_validation" if msg.kind == "final_validation" else "recruiter_reminder"
            link = V.app_link(base, purpose, health_check_id=e["check"].id if e["check"] else None,
                              job_id=item.get("job_id"))
            params = reminder_params(item, rule, e["day"], user.get("name"), link, today)
            fields = _log_fields(organization_id, msg.kind, keys[id(e)], item, rule, e["day"],
                                 rid, recipients, account, template, now_utc)
            fields["health_check_ids"] = [e["check"].id] if e["check"] else None
            fields["scheduled_for"] = local_to_utc_naive(
                today, parse_hhmm(rule.send_time, time(10, 0)), st.timezone)
        fields["params"] = params
        log, created = sender.create_log(db, **fields)
        if not created:
            continue
        sender.apply_prechecks(db, log, settings=st, account=account, template=template,
                               user=user)
        if log.status == "queued":
            stats["queued"] += 1
            to_dispatch.append(log.id)
        else:
            stats["skipped"] += 1
        db.commit()

    sender.dispatch_many(db, to_dispatch, dispatch)
    return stats


def _log_fields(organization_id, kind, key, item, rule: Optional[RuleSpec], day,
                recipient_user_id, recipients, account, template,
                now_utc: Optional[datetime]) -> Dict[str, Any]:
    item = item or {}
    user = recipients.get(recipient_user_id) or {}
    return {
        "organization_id": organization_id, "kind": kind, "idempotency_key": key,
        "account_id": account.id if account else None,
        "provider": account.provider if account else None,
        "rule_id": rule.id if rule else None, "rule_name": rule.name if rule else None,
        "template_id": template.id if template else (rule.template_id if rule else None),
        "template_name": template.name if template else None,
        "candidate_job_id": item.get("candidate_job_id"),
        "candidate_job_status_id": item.get("candidate_job_status_id"),
        "candidate_id": item.get("candidate_id"), "candidate_name": item.get("candidate_name"),
        "job_id": item.get("job_id"), "job_title": item.get("job_title"),
        "recipient_user_id": recipient_user_id, "recipient_name": user.get("name"),
        "recipient_type": "recruiter", "phone_e164": user.get("phone_e164"),
        "day_number": day,
        "scheduled_for": (now_utc or datetime.utcnow()).replace(tzinfo=None),
    }


def group_items_by_org(items: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    out: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for it in items:
        if it.get("organization_id") is not None and it.get("candidate_job_id") is not None:
            out[int(it["organization_id"])].append(it)
    return out


def enabled_org_ids(db: Session) -> List[int]:
    from app.whatsapp_layer.models import ClawbackNotificationSettings as S
    return [r[0] for r in db.query(S.organization_id).filter(S.enabled == 1).all()]


def run_reminders(db: Session, now_utc: Optional[datetime] = None,
                  dispatch: Optional[Callable[[int], None]] = None) -> Dict[str, int]:
    """evaluate_clawback_reminders: every enabled org, one job-service call."""
    from app.whatsapp_layer import clients
    totals = {"orgs": 0, "checks": 0, "queued": 0, "skipped": 0}
    orgs = enabled_org_ids(db)
    if not orgs:
        return totals
    payload = clients.fetch_active_clawbacks()
    by_org = group_items_by_org(payload.get("items") or [])
    for org_id in orgs:
        try:
            stats = evaluate_org_reminders(db, org_id, by_org.get(org_id, []), now_utc, dispatch)
            totals["orgs"] += 1
            for k in ("checks", "queued", "skipped"):
                totals[k] += stats[k]
        except Exception as exc:
            db.rollback()
            logger.error("Clawback reminders failed for org %s: %s", org_id, exc, exc_info=True)
    return totals


# ---------------------------------------------------------------------------
# Missed checks / state changes
# ---------------------------------------------------------------------------
def mark_missed(db: Session, now_utc: Optional[datetime] = None) -> int:
    """Pending checks become `missed` at the org's cutoff on their due date
    (and any older pending check, in case the service was down)."""
    from app.whatsapp_layer.models import ClawbackHealthCheck as H, ClawbackNotificationSettings as S
    total = 0
    org_ids = [r[0] for r in db.query(H.organization_id).filter(H.status == "pending")
               .distinct().all()]
    for org_id in org_ids:
        st = db.query(S).filter(S.organization_id == org_id).first()
        tz = st.timezone if st else DEFAULT_TIMEZONE
        cutoff = parse_hhmm(st.missed_check_cutoff_time if st else None, time(23, 0))
        now_local = local_now(tz, now_utc)
        today = now_local.date()
        last_due = today if now_local.time() >= cutoff else today - timedelta(days=1)
        n = (db.query(H).filter(H.organization_id == org_id, H.status == "pending",
                                H.due_date <= last_due)
             .update({H.status: "missed"}, synchronize_session=False))
        total += n
    db.commit()
    if total:
        logger.info("Marked %d clawback health checks missed", total)
    return total


CANCEL_CHANGES = {"completed", "rejected", "dropped"}
REFRESH_CHANGES = {"joined", "joined_updated"}


def handle_clawback_state_changed(db: Session, data: Dict[str, Any]) -> Dict[str, Any]:
    """`clawback_state_changed` from the job service (contract §3.1).

    completed / rejected / dropped → cancel every pending check.
    joined / joined_updated → the anchor or dates moved: cancel today's and
    future pending checks that no longer match the rules (the next run
    recreates the right one)."""
    from app.whatsapp_layer import clients
    from app.whatsapp_layer.models import ClawbackHealthCheck as H

    cj = data.get("candidate_job_id")
    org_id = data.get("organization_id")
    change = str(data.get("change") or "").strip().lower()
    if not cj:
        return {"cancelled": 0, "message": "candidate_job_id missing"}
    cj = int(cj)
    q = db.query(H).filter(H.candidate_job_id == cj, H.status == "pending")
    if org_id is not None:
        q = q.filter(H.organization_id == int(org_id))

    cancelled = 0
    if change in CANCEL_CHANGES:
        for chk in q.all():
            chk.status, chk.cancelled_reason = "cancelled", change
            cancelled += 1
    elif change in REFRESH_CHANGES:
        pending = q.all()
        if pending:
            org = int(org_id) if org_id is not None else pending[0].organization_id
            st = get_settings(db, org)
            today = local_now(st.timezone).date()
            rules = _rule_specs(active_rules(db, org))
            try:
                item = clients.fetch_clawback_item(org, cj)
            except Exception as exc:
                logger.warning("clawback_state_changed refresh: job service lookup failed: %s", exc)
                item = None
            for chk in pending:
                if chk.due_date < today:
                    continue
                keep = False
                if item:
                    anchor, day = anchor_and_day(item, chk.due_date)
                    rule = match_rule(rules, anchor, day, weekday_code(chk.due_date)) if anchor else None
                    if rule and rule.creates_health_check:
                        keep = True
                        chk.rule_id, chk.anchor, chk.day_number = rule.id, anchor, day
                        chk.stage_label = rule.stage_label or rule.name
                if not keep:
                    chk.status, chk.cancelled_reason = "cancelled", change
                    cancelled += 1
    db.commit()
    logger.info("clawback_state_changed candidate_job=%s change=%s → cancelled %d checks",
                cj, change, cancelled)
    return {"cancelled": cancelled, "message": f"clawback_state_changed handled ({change or 'unknown'})"}


# ---------------------------------------------------------------------------
# Send now / simulate
# ---------------------------------------------------------------------------
class ClawbackNotFound(Exception):
    pass


def _rule_for_manual(rules: List[RuleSpec], anchor: str, day: int, wd: str) -> Optional[RuleSpec]:
    """Today's due rule, else the rule whose window contains today."""
    rule = match_rule(rules, anchor, day, wd)
    if rule:
        return rule
    inside = [r for r in rules if r.is_active and r.anchor == anchor and in_window(r, day)]
    inside.sort(key=lambda r: (-r.priority, r.id or 0))
    return inside[0] if inside else None


def send_now(db: Session, organization_id: int, candidate_job_id: int,
             user_id: Optional[int]):
    """Manual reminder for one clawback (contract §2.8). Bypasses the schedule
    (send time, quiet hours, frequency) but is idempotent per day."""
    from app.whatsapp_layer import accounts as acct_svc, clients, people, sender
    from app.whatsapp_layer import variables as V
    from app.whatsapp_layer.models import ClawbackHealthCheck as H

    item = clients.fetch_clawback_item(organization_id, candidate_job_id)
    if not item or int(item.get("organization_id") or organization_id) != int(organization_id):
        raise ClawbackNotFound(f"No active clawback for candidate_job_id {candidate_job_id}")
    st = get_settings(db, organization_id)
    now_local = local_now(st.timezone)
    today, wd = now_local.date(), weekday_code(now_local.date())
    anchor, day = anchor_and_day(item, today)
    if anchor is None:
        raise ClawbackNotFound("The clawback has not started yet")
    rules = _rule_specs(active_rules(db, organization_id))
    rule = _rule_for_manual(rules, anchor, day, wd)
    purpose = "final_validation" if anchor == "cooling_end" else "recruiter_reminder"
    template = (sender.rule_template(db, rule.template_id) if rule and rule.template_id
                else sender.approved_template_for_purpose(db, organization_id, purpose))
    rid = recruiter_for(item, bool(st.fallback_to_created_by))
    users = people.get_users(db, [rid] if rid else [])
    account = acct_svc.get_active_account(db, organization_id)
    check = db.query(H).filter(H.candidate_job_id == int(candidate_job_id),
                               H.due_date == today).first()

    key = idempotency_key("manual", rule.id if rule else 0, candidate_job_id, rid, today)
    fields = _log_fields(organization_id, "manual", key, item, rule, day, rid, users,
                         account, template, None)
    fields.update({
        "triggered_by": "manual", "created_by": user_id,
        "health_check_ids": [check.id] if check else None,
        "params": reminder_params(
            item, rule or RuleSpec(None, "", anchor, 0, None, "once"), day,
            (users.get(rid) or {}).get("name"),
            V.app_link(st.app_base_url, purpose,
                       health_check_id=check.id if check else None, job_id=item.get("job_id")),
            today),
    })
    log, created = sender.create_log(db, **fields)
    if not created:
        return log
    if cooling_days(item) <= 0:
        sender.mark_skipped(log, "no_cooling_period")
    else:
        sender.apply_prechecks(db, log, settings=st, account=account, template=template,
                               user=users.get(rid), check_org_enabled=True)
    db.commit()
    if log.status == "queued":
        log = sender.send_log(db, log.id) or log
    return log


def simulate(db: Session, organization_id: int, candidate_job_id: int,
             days: int = 120) -> Dict[str, Any]:
    """Dry run (contract §2.8): nothing is written."""
    from app.whatsapp_layer import accounts as acct_svc, clients, people, sender

    item = clients.fetch_clawback_item(organization_id, candidate_job_id)
    if not item or int(item.get("organization_id") or organization_id) != int(organization_id):
        raise ClawbackNotFound(f"No active clawback for candidate_job_id {candidate_job_id}")
    from app.whatsapp_layer.models import ClawbackNotificationSettings as S
    st = db.query(S).filter(S.organization_id == organization_id).first()
    tz = st.timezone if st else DEFAULT_TIMEZONE
    today = local_now(tz).date()
    rules = _rule_specs(active_rules(db, organization_id))
    rid = recruiter_for(item, bool(st.fallback_to_created_by) if st else True)
    users = people.get_users(db, [rid] if rid else [])
    user = users.get(rid)
    account = acct_svc.get_active_account(db, organization_id)
    opted_out = sender.is_opted_out(db, organization_id, (user or {}).get("phone_e164"))
    templates: Dict[Optional[int], Any] = {}

    events = []
    for ev in project_schedule(rules, item, today, days):
        rule: RuleSpec = ev["rule"]
        if rule.template_id not in templates:
            templates[rule.template_id] = sender.rule_template(db, rule.template_id)
        template = templates[rule.template_id]
        if st is None or not st.enabled:
            reason = "disabled_org"
        elif cooling_days(item) <= 0:
            reason = "no_cooling_period"
        elif not is_working_day(ev["date"], st.working_days):
            reason = "non_working_day"
        else:
            reason = sender.skip_reason_for(settings=st, account=account, template=template,
                                            user=user, opted_out=opted_out)
        events.append({
            "date": ev["date"].isoformat(), "day_number": ev["day_number"],
            "anchor": ev["anchor"], "rule_id": rule.id, "rule_name": rule.name,
            "kind": "final_validation" if ev["anchor"] == "cooling_end" else "reminder",
            "template_name": template.name if template else None,
            "recipient_user_id": rid, "recipient_name": (user or {}).get("name"),
            "skip_reason": reason,
        })
    return {"clawback": item, "events": events}
