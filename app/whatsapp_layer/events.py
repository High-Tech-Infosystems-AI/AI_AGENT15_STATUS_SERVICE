"""Events on/off (contract §6.2): one flat list of everything the clawback
notifications can send — recruiter rules, final validation, the combined
recruiter message, team digests and the in-app admin alerts.

Keys:
  rule:<id>    → clawback_notification_rules.is_active
  digest:<id>  → clawback_digest_rules.is_active
  system:<x>   → clawback_notification_settings.event_toggles[x] (missing = on)

Pure helpers first (labels, schedule summaries, toggle lookup); DB glue
(list / set) follows.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

GROUPS = ["recruiter_reminders", "final_validation", "recruiter_summary",
          "team_summaries", "admin_alerts"]

RECRUITER_SUMMARY = "recruiter_summary"
ALERT_FAILURE_RATE = "alert_failure_rate"
ALERT_TEMPLATE_REJECTED = "alert_template_rejected"
ALERT_ACCOUNT_ERROR = "alert_account_error"

ADMIN_RECIPIENTS = "Org admins (in-app)"

# name → (label, description); order = listing order inside admin_alerts.
ADMIN_ALERTS: Dict[str, Dict[str, str]] = {
    ALERT_FAILURE_RATE: {
        "label": "Messages failing",
        "description": "Alert when more than 10% of WhatsApp messages failed in the last hour",
    },
    ALERT_TEMPLATE_REJECTED: {
        "label": "Template rejected",
        "description": "Alert when Meta rejects a WhatsApp template",
    },
    ALERT_ACCOUNT_ERROR: {
        "label": "Account error",
        "description": "Alert when the WhatsApp provider rejects the login or access token",
    },
}
EOD_REPORT = "eod_report"
SYSTEM_EVENTS = [RECRUITER_SUMMARY, EOD_REPORT] + list(ADMIN_ALERTS)

_DAY_NAMES = {"MON": "Mon", "TUE": "Tue", "WED": "Wed", "THU": "Thu", "FRI": "Fri",
              "SAT": "Sat", "SUN": "Sun"}

AUDIENCE_LABELS = {
    "team_managers": "Team managers",
    "has_subordinate_managers": "Senior managers",
    "designation_levels": "Designation levels",
    "roles": "Selected roles",
    "users": "Selected users",
    "admins": "Admins and super admins",
}


# ---------------------------------------------------------------------------
# Pure: toggles
# ---------------------------------------------------------------------------
def toggle_enabled(toggles: Optional[Dict[str, Any]], name: str) -> bool:
    """Missing key / NULL column → enabled."""
    if not isinstance(toggles, dict) or name not in toggles:
        return True
    return bool(toggles[name])


def system_enabled(settings, name: str) -> bool:
    """`settings` is a ClawbackNotificationSettings row (or None → enabled)."""
    return toggle_enabled(getattr(settings, "event_toggles", None) if settings else None, name)


def is_system_enabled(db: Session, organization_id: int, name: str) -> bool:
    """Read-only check (never creates the settings row). Errors → enabled, so
    a broken lookup never silences an alert."""
    from app.whatsapp_layer.models import ClawbackNotificationSettings as S
    try:
        st = db.query(S).filter(S.organization_id == organization_id).first()
    except Exception as exc:
        logger.warning("Event toggle lookup failed for org %s (%s); assuming on",
                       organization_id, exc)
        return True
    return system_enabled(st, name)


# ---------------------------------------------------------------------------
# Pure: human-friendly text
# ---------------------------------------------------------------------------
def _days_list(fv: Dict[str, Any]) -> List[int]:
    out = []
    for d in fv.get("days") or []:
        try:
            out.append(int(d))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _weekday_names(values) -> List[str]:
    order = list(_DAY_NAMES)
    codes = [str(w).upper() for w in values or [] if str(w).upper() in _DAY_NAMES]
    codes = sorted(set(codes), key=order.index)
    return [_DAY_NAMES[c] for c in codes]


def _and_join(parts: List[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _n(fv: Dict[str, Any]) -> int:
    try:
        return max(1, int(fv.get("n") or 1))
    except (TypeError, ValueError):
        return 1


def rule_schedule_summary(frequency_type: str, frequency_value: Optional[Dict[str, Any]],
                          send_time: Optional[str]) -> str:
    """"Every 2 days · 10:00", "Days 1, 3, 7 · 10:00", "Mon, Thu · 10:00",
    "Weekly · 10:00", "Once · 10:00"."""
    fv = frequency_value or {}
    if frequency_type == "every_n_days":
        n = _n(fv)
        head = "Daily" if n == 1 else f"Every {n} days"
    elif frequency_type == "specific_days":
        days = _days_list(fv)
        head = ("Day " if len(days) == 1 else "Days ") + ", ".join(str(d) for d in days)
    elif frequency_type == "weekdays":
        head = ", ".join(_weekday_names(fv.get("weekdays"))) or "Weekdays"
    elif frequency_type == "weekly":
        head = "Weekly"
    elif frequency_type == "once":
        head = "Once"
    else:
        head = str(frequency_type or "-")
    return f"{head} · {send_time}" if send_time else head


def digest_schedule_summary(frequency_type: str, frequency_value: Optional[Dict[str, Any]],
                            send_time: Optional[str]) -> str:
    """"Daily · 09:30", "Weekly on Mon · 09:30", "Mon, Thu · 09:30"."""
    fv = frequency_value or {}
    if frequency_type == "daily":
        head = "Daily"
    elif frequency_type == "weekly":
        names = _weekday_names([fv.get("weekday")])
        head = f"Weekly on {names[0]}" if names else "Weekly"
    elif frequency_type == "weekdays":
        head = ", ".join(_weekday_names(fv.get("weekdays"))) or "Weekdays"
    else:
        head = str(frequency_type or "-")
    return f"{head} · {send_time}" if send_time else head


def _frequency_phrase(frequency_type: str, fv: Dict[str, Any]) -> str:
    if frequency_type == "every_n_days":
        n = _n(fv)
        return "daily" if n == 1 else f"every {n} days"
    if frequency_type == "weekdays":
        return "every " + (_and_join(_weekday_names(fv.get("weekdays"))) or "weekday")
    if frequency_type == "weekly":
        return "weekly"
    return str(frequency_type or "")


def rule_description(anchor: str, start_day: int, end_day: Optional[int],
                     frequency_type: str, frequency_value: Optional[Dict[str, Any]]) -> str:
    """e.g. "Health-check reminder to the recruiter, weekly from day 8 to day 30"."""
    fv = frequency_value or {}
    if anchor == "cooling_end":
        lead = "Final status and eligibility validation request to the recruiter"
        if frequency_type == "once" and start_day == 0:
            return f"{lead}, on the day the cooling period ends"
        if frequency_type == "once":
            return f"{lead}, {start_day} days after the cooling period ends"
        if frequency_type == "specific_days":
            days = _and_join([str(d) for d in _days_list(fv)])
            return f"{lead}, on days {days} after the cooling period ends"
        span = (f"from day {start_day} to day {end_day}" if end_day is not None
                else f"from day {start_day}")
        return f"{lead}, {_frequency_phrase(frequency_type, fv)} {span} after the cooling period ends"

    lead = "Health-check reminder to the recruiter"
    pre = anchor == "offer_accepted"
    after = "after the offer is accepted" if pre else "after joining"
    if frequency_type == "specific_days":
        days = _days_list(fv)
        word = "day" if len(days) == 1 else "days"
        return f"{lead}, on {word} {_and_join([str(d) for d in days])} {after}"
    if frequency_type == "once":
        return f"{lead}, once on day {start_day} {after}"
    freq = _frequency_phrase(frequency_type, fv)
    if pre:
        start = "from offer acceptance" if start_day <= 1 else f"from day {start_day} {after}"
        end = f" to day {end_day}" if end_day is not None else " until joining"
        return f"{lead}, {freq} {start}{end}"
    end = f" to day {end_day}" if end_day is not None else " until the cooling period ends"
    return f"{lead}, {freq} from day {start_day}{end}"


def digest_description(audience_type: str, audience_value: Optional[Dict[str, Any]],
                       scope: Optional[str]) -> str:
    av = audience_value or {}
    if audience_type == "team_managers":
        who = "team managers"
    elif audience_type == "has_subordinate_managers":
        who = "managers of managers"
    elif audience_type == "designation_levels":
        lo, hi = av.get("min_level"), av.get("max_level")
        if lo is not None and hi is not None and lo != hi:
            who = f"designation levels {lo}–{hi}"
        elif lo is not None or hi is not None:
            who = f"designation level {lo if lo is not None else hi}"
        else:
            who = "selected designation levels"
    elif audience_type == "roles":
        who = "selected roles"
    elif audience_type == "users":
        who = "selected users"
    elif audience_type == "admins":
        who = "admins and super admins"
    else:
        who = str(audience_type or "managers")
    if scope == "organisation" or audience_type == "admins":
        covering = "the whole organisation"
    else:
        covering = "their direct team" if scope == "direct_team" else "their whole team"
    return f"Clawback summary to {who}, covering {covering}"


def digest_recipients(audience_type: str) -> str:
    return AUDIENCE_LABELS.get(audience_type, "Managers")


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------
def _template_ref(tpl) -> Optional[Dict[str, Any]]:
    if tpl is None:
        return None
    return {"id": tpl.id, "name": tpl.name, "approval_status": tpl.approval_status}


def rule_event(r, tpl) -> Dict[str, Any]:
    final = r.anchor == "cooling_end"
    return {
        "key": f"rule:{r.id}", "type": "rule", "ref_id": r.id,
        "group": "final_validation" if final else "recruiter_reminders",
        "label": r.stage_label or r.name,
        "description": rule_description(r.anchor, int(r.start_day or 0), r.end_day,
                                        r.frequency_type, r.frequency_value),
        "enabled": bool(r.is_active), "template": _template_ref(tpl),
        "schedule_summary": rule_schedule_summary(r.frequency_type, r.frequency_value,
                                                  r.send_time),
        "recipients": "Recruiter",
    }


def digest_event(r, tpl) -> Dict[str, Any]:
    return {
        "key": f"digest:{r.id}", "type": "digest", "ref_id": r.id, "group": "team_summaries",
        "label": r.name,
        "description": digest_description(r.audience_type, r.audience_value, r.scope),
        "enabled": bool(r.is_active), "template": _template_ref(tpl),
        "schedule_summary": digest_schedule_summary(r.frequency_type, r.frequency_value,
                                                    r.send_time),
        "recipients": digest_recipients(r.audience_type),
    }


def recruiter_summary_event(settings, tpl, send_times: List[str]) -> Dict[str, Any]:
    threshold = max(0, int(getattr(settings, "recruiter_batch_threshold", None) or 0))
    when = ("" if threshold == 0 else f" when more than {threshold} are due")
    times = sorted({t for t in send_times if t})
    return {
        "key": f"system:{RECRUITER_SUMMARY}", "type": "system", "ref_id": None,
        "group": "recruiter_summary", "label": "Daily candidate list",
        "description": ("One message per recruiter listing every candidate due that day, with "
                        "company, joining date, day count, what to do and a link to each "
                        f"health check{when}. Turned off, each candidate gets its own message"),
        "enabled": system_enabled(settings, RECRUITER_SUMMARY), "template": _template_ref(tpl),
        "schedule_summary": f"Daily · {times[0]}" if times else "Daily",
        "recipients": "Recruiter",
    }


def alert_event(settings, name: str) -> Dict[str, Any]:
    spec = ADMIN_ALERTS[name]
    return {
        "key": f"system:{name}", "type": "system", "ref_id": None, "group": "admin_alerts",
        "label": spec["label"], "description": spec["description"],
        "enabled": system_enabled(settings, name), "template": None,
        "schedule_summary": "When it happens", "recipients": ADMIN_RECIPIENTS,
    }


# ---------------------------------------------------------------------------
# DB glue
# ---------------------------------------------------------------------------
class EventNotFound(Exception):
    pass


def _rules(db: Session, organization_id: int) -> List:
    from app.whatsapp_layer import clawback_engine as E
    from app.whatsapp_layer.models import ClawbackNotificationRule as R
    rows = db.query(R).filter(R.organization_id == organization_id, R.deleted_at.is_(None)).all()
    rows.sort(key=lambda r: (E.ANCHORS.index(r.anchor) if r.anchor in E.ANCHORS else 9,
                             r.start_day, r.id))
    return rows


def _digests(db: Session, organization_id: int) -> List:
    from app.whatsapp_layer.models import ClawbackDigestRule as D
    return (db.query(D).filter(D.organization_id == organization_id, D.deleted_at.is_(None))
            .order_by(D.priority.desc(), D.id.asc()).all())


def _templates_by_id(db: Session, ids) -> Dict[int, Any]:
    from app.whatsapp_layer.models import WhatsAppTemplate as T
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {t.id: t for t in db.query(T).filter(T.id.in_(ids), T.deleted_at.is_(None)).all()}


def _approved_batch_template(db: Session, organization_id: int):
    from app.whatsapp_layer.models import WhatsAppTemplate as T
    return (db.query(T).filter(T.organization_id == organization_id,
                               T.purpose == "recruiter_batch", T.approval_status == "approved",
                               T.deleted_at.is_(None), T.is_active == 1)
            .order_by(T.version.desc(), T.id.desc()).first())


def eod_event(db: Session, organization_id: int, settings) -> Dict[str, Any]:
    from app.whatsapp_layer import eod_report
    from app.whatsapp_layer.models import WhatsAppTemplate as T
    tpl = (db.query(T).filter(T.organization_id == organization_id,
                              T.name == eod_report.TEMPLATE_NAME, T.deleted_at.is_(None)).first())
    return {
        "key": f"system:{EOD_REPORT}", "type": "system", "ref_id": None,
        "group": "team_summaries", "label": "End of day report",
        "description": ("What each recruiter did today, which candidates weren't tracked, and "
                        "who was dropped or is at risk"),
        "enabled": system_enabled(settings, EOD_REPORT), "template": _template_ref(tpl),
        "schedule_summary": f"Daily · {eod_report.EOD_TIME}",
        "recipients": "Team leads, senior managers and admins",
    }


def system_event(db: Session, organization_id: int, settings, name: str) -> Dict[str, Any]:
    if name == EOD_REPORT:
        return eod_event(db, organization_id, settings)
    if name == RECRUITER_SUMMARY:
        times = [r.send_time for r in _rules(db, organization_id)
                 if r.is_active and r.anchor != "cooling_end"]
        return recruiter_summary_event(settings, _approved_batch_template(db, organization_id),
                                       times)
    return alert_event(settings, name)


def list_events(db: Session, organization_id: int) -> List[Dict[str, Any]]:
    """Ordered by group (GROUPS order), then rule order."""
    from app.whatsapp_layer import clawback_engine as E
    st = E.get_settings(db, organization_id)
    rules, digests = _rules(db, organization_id), _digests(db, organization_id)
    tmap = _templates_by_id(db, [r.template_id for r in rules + digests])
    reminders = [rule_event(r, tmap.get(r.template_id)) for r in rules
                 if r.anchor != "cooling_end"]
    finals = [rule_event(r, tmap.get(r.template_id)) for r in rules
              if r.anchor == "cooling_end"]
    out = reminders + finals
    out.append(system_event(db, organization_id, st, RECRUITER_SUMMARY))
    out.extend(digest_event(d, tmap.get(d.template_id)) for d in digests)
    out.append(eod_event(db, organization_id, st))
    out.extend(alert_event(st, name) for name in ADMIN_ALERTS)
    return out


def parse_key(key: str):
    """"rule:3" → ("rule", 3); "system:x" → ("system", "x"). Unknown → EventNotFound."""
    kind, _, ref = str(key or "").partition(":")
    if kind in ("rule", "digest"):
        try:
            return kind, int(ref)
        except ValueError:
            raise EventNotFound(key)
    if kind == "system" and ref in SYSTEM_EVENTS:
        return kind, ref
    raise EventNotFound(key)


def set_system_toggle(settings, name: str, enabled: bool, user_id: Optional[int]) -> None:
    """Reassign the dict so SQLAlchemy sees the JSON change. Caller commits."""
    toggles = dict(settings.event_toggles or {})
    toggles[name] = bool(enabled)
    settings.event_toggles = toggles
    settings.updated_by = user_id
