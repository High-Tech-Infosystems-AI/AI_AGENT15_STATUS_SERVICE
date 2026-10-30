"""Today's clawback check-ins (contract §6.7): the data behind the /clawback/today
page and the formatted list sent when someone taps "Show list" on WhatsApp.

Plain text messages may have line breaks but WhatsApp only delivers them in the
24 hours after the person last wrote to us, and tapping the button opens that
window. So the reminder is a short template and the list is sent as its reply.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import digest_engine as D
from app.whatsapp_layer import variables as V

logger = logging.getLogger("app_logger")

SHOW_LIST_WORDS = {"show list", "show my list", "list"}
TEXT_MAX_CHARS = 3900   # WhatsApp allows 4096 for a text message


def _progress(item: Optional[Dict[str, Any]], today: date) -> Dict[str, Any]:
    """Where the clawback period stands: joining date, completion, days left, %."""
    if not item:
        return {"joined_at": None, "offer_accepted_at": None, "completion_date": None,
                "cooling_period_days": None, "days_pending": None, "progress_percent": None}
    joined, comp, cp = E.parse_date(item.get("joined_at")), E.completion_date_of(item), E.cooling_days(item)
    percent = None
    if joined and cp > 0:
        percent = max(0, min(100, round((today - joined).days / cp * 100)))
    return {"joined_at": item.get("joined_at"), "offer_accepted_at": item.get("offer_accepted_at"),
            "completion_date": comp.isoformat() if comp else None,
            "cooling_period_days": cp if cp > 0 else None,
            "days_pending": max(0, (comp - today).days) if comp else None,
            "progress_percent": percent}


def _candidate(e: Dict[str, Any], base_url: Optional[str], today: date) -> Dict[str, Any]:
    item, chk = e["item"], e.get("check")
    cp = E.cooling_days(item)
    return {
        **_progress(item, today),
        "check_id": chk.id if chk is not None else None,
        "candidate_job_id": item.get("candidate_job_id"),
        "candidate_id": item.get("candidate_id"),
        "candidate_name": item.get("candidate_name") or "-",
        "company_name": item.get("company_name") or "-",
        "job_id": item.get("job_id"),
        "job_title": item.get("job_title") or "-",
        "day_number": e.get("day"),
        "progress": V.progress_text(
            e.get("anchor"), joining_date=E.parse_date(item.get("joined_at")),
            day_number=e.get("day"), cooling_period_days=str(cp) if cp > 0 else "-",
            offer_accepted_date=E.parse_date(item.get("offer_accepted_at")),
            completion_date=E.completion_date_of(item)),
        "action": E.action_for(e.get("rule")),
        "status": chk.status if chk is not None else None,
        "outcome": chk.outcome if chk is not None else None,
        "notes": chk.notes if chk is not None else None,
        "due_date": chk.due_date.isoformat() if chk is not None and chk.due_date else None,
        "overdue": False,
        "link": V.check_link(base_url, chk.id if chk is not None else None),
    }


def _overdue(db: Session, org: int, today: date, base_url: Optional[str],
             scope: Optional[Set[int]], items_by_cj: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    from app.whatsapp_layer.models import ClawbackHealthCheck as H
    q = db.query(H).filter(H.organization_id == org, H.status.in_(("pending", "missed")),
                           H.due_date < today)
    if scope is not None:
        q = q.filter(H.recruiter_user_id.in_(list(scope)))
    return [{
        **_progress(items_by_cj.get(c.candidate_job_id), today),
        "check_id": c.id, "candidate_job_id": c.candidate_job_id, "candidate_id": c.candidate_id,
        "candidate_name": c.candidate_name or "-", "company_name": c.company_name or "-",
        "job_id": c.job_id, "job_title": c.job_title or "-", "day_number": c.day_number,
        "progress": f"was due {c.due_date.strftime('%d %b')}", "action": c.stage_label or "Check-in",
        "status": c.status, "outcome": c.outcome, "notes": c.notes,
        "due_date": c.due_date.isoformat(), "overdue": True,
        "link": V.check_link(base_url, c.id), "recruiter_user_id": c.recruiter_user_id,
    } for c in q.order_by(H.due_date.asc(), H.id.asc()).limit(500).all()]


def _checks_between(db: Session, org: int, start: date, end: date, today: date,
                    base_url: Optional[str], scope: Optional[Set[int]],
                    items_by_cj: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The checks that were due in a date range, with their status now."""
    from app.whatsapp_layer.models import ClawbackHealthCheck as H
    q = db.query(H).filter(H.organization_id == org, H.due_date >= start, H.due_date <= end,
                           H.status != "cancelled")
    if scope is not None:
        q = q.filter(H.recruiter_user_id.in_(list(scope)))
    return [{
        **_progress(items_by_cj.get(c.candidate_job_id), today),
        "check_id": c.id, "candidate_job_id": c.candidate_job_id, "candidate_id": c.candidate_id,
        "candidate_name": c.candidate_name or "-", "company_name": c.company_name or "-",
        "job_id": c.job_id, "job_title": c.job_title or "-", "day_number": c.day_number,
        "progress": f"was due {c.due_date.strftime('%d %b')}", "action": c.stage_label or "Check-in",
        "status": c.status, "outcome": c.outcome, "notes": c.notes,
        "due_date": c.due_date.isoformat(),
        "overdue": c.due_date < today and c.status in ("pending", "missed"),
        "link": V.check_link(base_url, c.id), "recruiter_user_id": c.recruiter_user_id,
    } for c in q.order_by(H.due_date.desc(), H.id.asc()).limit(1000).all()]


def build_today(db: Session, org: int, scope: Optional[Set[int]],
                date_from: Optional[date] = None, date_to: Optional[date] = None) -> Dict[str, Any]:
    """Check-ins for a date range (default today), grouped by recruiter. A range
    that includes today also has today's due candidates, with their checks
    created if not there yet, and earlier ones still open. `scope` None = whole org."""
    from app.whatsapp_layer import clients, people
    st = E.get_settings(db, org)
    today = E.local_now(st.timezone).date()
    start, end = date_from or today, date_to or date_from or today
    includes_today = start <= today <= end
    items = clients.fetch_active_clawbacks(org).get("items") or []
    base = st.app_base_url
    by_rec: Dict[Optional[int], List[Dict[str, Any]]] = {}
    seen: Set[int] = set()
    if includes_today:
        rules = [E.as_rule_spec(r) for r in E.active_rules(db, org)]
        due = D.due_today(db, org, items, rules, today, bool(st.fallback_to_created_by))
        db.commit()
        for e in due:
            rid = e["recruiter_user_id"]
            if scope is None or (rid is not None and int(rid) in scope):
                c = _candidate(e, base, today)
                seen.add(c["check_id"])
                by_rec.setdefault(rid, []).append(c)
    items_by_cj = {int(i["candidate_job_id"]): i for i in items if i.get("candidate_job_id")}
    others = _checks_between(db, org, start, end, today, base, scope, items_by_cj)
    if includes_today:
        others += _overdue(db, org, today, base, scope, items_by_cj)
    for c in others:
        if c["check_id"] not in seen:
            seen.add(c["check_id"])
            by_rec.setdefault(c.pop("recruiter_user_id"), []).append(c)
    users = people.get_users(db, [r for r in by_rec if r])
    groups = []
    for rid, cands in by_rec.items():
        u = users.get(int(rid)) if rid else None
        groups.append({
            "recruiter_user_id": rid, "recruiter_name": (u or {}).get("name") or "No recruiter assigned",
            "has_phone": bool((u or {}).get("phone_e164")), "candidates": cands,
            "open_count": sum(1 for c in cands if c["status"] in (None, "pending", "missed")),
            "done_count": sum(1 for c in cands if c["status"] == "done"),
        })
    groups.sort(key=lambda g: (g["recruiter_name"] or "").lower())
    return {"date": today.isoformat(), "date_from": start.isoformat(),
            "date_to": end.isoformat(), "groups": groups}


def _entry_text(n: int, c: Dict[str, Any]) -> str:
    lines = [f"*{n}. {c['candidate_name']}*", c["company_name"], c["progress"],
             f"To do: {c['action']}"]
    if c.get("check_id"):
        lines.append(c["link"])
    return "\n".join(lines)


def render_list(groups: List[Dict[str, Any]], show_recruiters: bool) -> List[str]:
    """Open check-ins as formatted text, split into messages WhatsApp accepts."""
    blocks: List[str] = []
    total = 0
    for g in groups:
        cands = [c for c in g["candidates"] if c["status"] in (None, "pending", "missed")]
        if not cands:
            continue
        total += len(cands)
        if show_recruiters:
            blocks.append(f"*━ {g['recruiter_name']} ({len(cands)}) ━*")
        blocks.extend(_entry_text(n, c) for n, c in enumerate(cands, start=1))
    if not blocks:
        return ["No clawback check-ins are open for today. Nothing to do."]
    head = f"*Clawback check-ins ({total})*"
    messages, current = [], head
    for block in blocks:
        if len(current) + len(block) + 2 > TEXT_MAX_CHARS:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"
    messages.append(current)
    return messages


def scope_for_user(db: Session, org: int, user_id: int) -> Optional[Set[int]]:
    """Whose list a person sees: admins everyone, others themselves and their team."""
    from app.whatsapp_layer import clients, people
    if any(a["user_id"] == user_id for a in people.admin_users(db, org)):
        return None
    try:
        rec = clients.subordinates_of(org, user_id)
        return {user_id} | {int(x) for x in (rec or {}).get("subordinate_user_ids") or []}
    except Exception as exc:
        logger.warning("Show list: hierarchy lookup failed for user %s: %s", user_id, exc)
        return {user_id}


def send_list_reply(db: Session, account, phone_digits: str, message_id: Optional[str]) -> List[int]:
    """Someone tapped "Show list": reply with their list as plain text. A number
    that isn't an HRMIS user gets nothing. Returns the created log ids."""
    from app.whatsapp_layer import accounts as acct_svc, people, sender
    from app.whatsapp_layer.providers.registry import get_provider
    org = account.organization_id
    user = people.find_by_phone(db, org, phone_digits)
    if user is None:
        logger.info("Show list from a number that is not an HRMIS user; ignored")
        return []
    scope = scope_for_user(db, org, user["id"])
    data = build_today(db, org, scope)
    many = scope is None or len(scope) > 1
    ref = message_id or f"{phone_digits}|{data['date']}"
    out: List[int] = []
    provider = get_provider(account.provider)
    creds = acct_svc.credentials(account)
    for n, text in enumerate(render_list(data["groups"], many), start=1):
        key = hashlib.sha1(f"show_list|{ref}|{n}".encode()).hexdigest()
        log, created = sender.create_log(
            db, organization_id=org, kind="manual", idempotency_key=key, account_id=account.id,
            provider=account.provider, rule_name="Show list reply", recipient_user_id=user["id"],
            recipient_name=user.get("name"), recipient_type="recruiter",
            phone_e164=user.get("phone_e164"), scheduled_for=datetime.utcnow(), rendered_body=text)
        if not created:
            continue
        result = provider.send_text(creds, phone_digits, text, f"cbk_list_{log.id}")
        sender.apply_send_result(log, result, datetime.utcnow(), f"cbk_list_{log.id}")
        db.commit()
        out.append(log.id)
    return out


def send_prompt(db: Session, org: int, recruiter_user_id: int, ctx_user_id: int):
    """A manager's "Notify": the recruiter's reminder, sent now."""
    from app.whatsapp_layer import accounts as acct_svc, clients, people, sender
    st = E.get_settings(db, org)
    today = E.local_now(st.timezone).date()
    items = [i for i in clients.fetch_active_clawbacks(org).get("items") or []
             if E.recruiter_for(i, bool(st.fallback_to_created_by)) == recruiter_user_id]
    rules = [E.as_rule_spec(r) for r in E.active_rules(db, org)]
    due = D.due_today(db, org, items, rules, today, bool(st.fallback_to_created_by))
    db.commit()
    data = build_today(db, org, {recruiter_user_id})
    open_count = sum(g["open_count"] for g in data["groups"])
    if not open_count:
        return None
    template = sender.approved_template_for_purpose(db, org, "recruiter_batch")
    account = acct_svc.get_active_account(db, org)
    user = people.get_users(db, [recruiter_user_id]).get(recruiter_user_id) or {}
    overdue = sum(1 for g in data["groups"] for c in g["candidates"] if c["overdue"] and c["status"] != "done")
    part = E.message_parts(template, E.candidate_list_parts(due, st.app_base_url))[0] if due else {
        "candidate_count": "0", "part_label": "0 candidates", "candidate_list": "-",
        "candidates": [], "health_check_ids": []}
    part.pop("health_check_ids", None)
    return _send_manual(db, org, st, account, template, recruiter_user_id, user, ctx_user_id, "notify", {
        "recruiter_name": user.get("name") or "-", "due_count": str(len(due)),
        "overdue_count": str(overdue), **part,
        "candidate_names": V.names_list([c["candidate_name"] for g in data["groups"]
                                         for c in g["candidates"] if not c["overdue"]]),
        "app_link": V.day_link(st.app_base_url, "recruiter_batch", today)})


def send_note(db: Session, org: int, recruiter_user_id: int, ctx_user_id: int, note: str):
    """A manager's "Send message": a short note through the note template."""
    from app.whatsapp_layer import accounts as acct_svc, people, sender
    from app.whatsapp_layer.models import WhatsAppTemplate as T
    st = E.get_settings(db, org)
    template = (db.query(T).filter(T.organization_id == org, T.name == "clawback_manager_note",
                                   T.deleted_at.is_(None)).first())
    account = acct_svc.get_active_account(db, org)
    names = people.get_users(db, [recruiter_user_id, ctx_user_id])
    user = names.get(recruiter_user_id) or {}
    return _send_manual(db, org, st, account, template, recruiter_user_id, user, ctx_user_id, "note", {
        "recruiter_name": user.get("name") or "-",
        "sender_name": (names.get(ctx_user_id) or {}).get("name") or "Your manager",
        "note": V.sanitize_text(note),
        "app_link": V.day_link(st.app_base_url, "recruiter_batch", E.local_now(st.timezone).date())})


def _send_manual(db, org, st, account, template, rid, user, by, what, params):
    from app.whatsapp_layer import sender
    now = datetime.utcnow()
    key = hashlib.sha1(f"today_{what}|{rid}|{by}|{now.isoformat()}".encode()).hexdigest()
    log, _ = sender.create_log(
        db, organization_id=org, kind="manual", idempotency_key=key,
        account_id=account.id if account else None, provider=account.provider if account else None,
        rule_name="Message from Today's check-ins", template_id=template.id if template else None,
        template_name=template.name if template else None, recipient_user_id=rid,
        recipient_name=user.get("name"), recipient_type="recruiter", phone_e164=user.get("phone_e164"),
        scheduled_for=now, triggered_by="manual", params=params)
    sender.apply_prechecks(db, log, settings=st, account=account, template=template, user=user or None)
    db.commit()
    if log.status == "queued":
        sender.send_log(db, log.id)
    db.refresh(log)
    return log
