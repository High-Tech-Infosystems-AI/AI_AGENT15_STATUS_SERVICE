"""Default seeds per org (contract §4): applied the first time an org enables
clawback notifications with no rules, or by reset-defaults.

Templates are created as drafts (en_US, UTILITY); the engine skips with
`template_not_approved` until each is approved.

The list templates (contract §6.4) carry a text header; the others have none.

Deviation: the contract's template bodies end with `{{app_link}}`, but the
same contract (and Meta) forbid a body ending with a variable, so the seeds
end with "{{app_link}}." — wording otherwise unchanged.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.whatsapp_layer import templates_service as ts
from app.whatsapp_layer import variables as V

logger = logging.getLogger("app_logger")

DEFAULT_TEMPLATES: List[Dict] = [
    {
        "name": "clawback_health_check_due", "purpose": "recruiter_reminder",
        "body_text": ("Hi {{recruiter_name}}, a health check is due today for {{candidate_name}} "
                      "({{job_title}}, {{company_name}}). They are on day {{day_number}} of "
                      "{{cooling_period_days}}. Please speak to them and record the outcome "
                      "here: {{app_link}}."),
    },
    {
        "name": "clawback_daily_checklist", "purpose": "recruiter_batch",
        # Contract §6.4: one list per recruiter per day (split into parts).
        "header_text": "Today's clawback check-ins",
        "body_text": ("Hi {{recruiter_name}}, here are your clawback check-ins for today "
                      "({{part_label}}):\n\n{{candidate_list}}\n\nStill pending from earlier "
                      "days: {{overdue_count}}. Open each link to see the candidate's details "
                      "and record how the check-in went."),
    },
    {
        "name": "clawback_final_validation_due", "purpose": "final_validation",
        "body_text": ("Hi {{recruiter_name}}, the cooling period for {{candidate_name}} "
                      "({{job_title}}, {{company_name}}) ended on {{clawback_completion_date}}. "
                      "Please complete the final status and eligibility validation: {{app_link}}."),
    },
    {
        "name": "clawback_team_checklist", "purpose": "team_digest",
        "header_text": "Team clawback check-ins",
        "body_text": ("Hi {{recipient_name}}, here are today's clawback check-ins for your team "
                      "({{part_label}}), grouped by recruiter. Checks missed "
                      "({{period_label}}): {{missed_count}}.\n\n{{candidate_list}}\n\n"
                      "Each link opens that candidate's health check."),
    },
]

_FINAL = "final_validation"

# Short reminders with a "Show list" button (contract §6.7): the formatted list
# is sent as the reply. Used in place of the list templates once approved.
SHOW_LIST_BUTTON = [{"type": "QUICK_REPLY", "label": "Show list"}]
DEFAULT_TEMPLATES += [
    {
        "name": "clawback_daily_prompt", "purpose": "recruiter_batch",
        "header_text": "Today's clawback check-ins", "buttons": SHOW_LIST_BUTTON,
        "body_text": ("Hi {{recruiter_name}}, you have {{due_count}} clawback check-ins for today "
                      "and {{overdue_count}} pending from earlier days.\n\nTap Show list to see "
                      "them here, or open and update them at {{app_link}}."),
    },
    {
        "name": "clawback_team_prompt", "purpose": "team_digest",
        "header_text": "Team clawback check-ins", "buttons": SHOW_LIST_BUTTON,
        "body_text": ("Hi {{recipient_name}}, your team has {{due_count}} clawback check-ins for "
                      "today across {{recruiter_count}} recruiters, and {{missed_count}} missed "
                      "({{period_label}}).\n\nTap Show list to see them here, or open them at "
                      "{{app_link}}."),
    },
    {
        "name": "clawback_manager_note", "purpose": "generic",
        "body_text": ("Hi {{recruiter_name}}, {{sender_name}} sent you a note about your clawback "
                      "check-ins: {{note}}\n\nOpen your list at {{app_link}}."),
    },
]

# Names instead of the list (contract §6.9); the newest approved template is used.
DEFAULT_TEMPLATES += [
    {
        "name": "clawback_daily_names", "purpose": "recruiter_batch",
        "header_text": "Today's clawback check-ins", "buttons": SHOW_LIST_BUTTON,
        "body_text": ("Hi {{recruiter_name}}, you have {{due_count}} clawback check-ins for today: "
                      "{{candidate_names}}.\n\nPending from earlier days: {{overdue_count}}.\n\n"
                      "Open and update them at {{app_link}}, or tap Show list for details."),
    },
    {
        "name": "clawback_team_names", "purpose": "team_digest",
        "header_text": "Team clawback check-ins", "buttons": SHOW_LIST_BUTTON,
        "body_text": ("Hi {{recipient_name}}, your team has {{due_count}} clawback check-ins for "
                      "today: {{recruiter_summary}}.\n\nMissed ({{period_label}}): {{missed_count}}."
                      "\n\nSee the team's progress at {{app_link}}, or tap Show list for details."),
    },
    {
        "name": "clawback_eod_report", "purpose": "generic",
        "header_text": "Clawback end of day report",
        "body_text": ("Hi {{recipient_name}}, here is the clawback report for {{report_date}}. "
                      "Check-ins done: {{done_count}} of {{total_count}}.\n\n"
                      "By recruiter: {{recruiter_progress}}.\n\n"
                      "Not tracked ({{untracked_count}}): {{untracked_list}}.\n\n"
                      "Flagged ({{flagged_count}}): {{flagged_list}}.\n\n"
                      "See the details at {{app_link}}."),
    },
]

DEFAULT_RULES: List[Dict] = [
    {"name": "Offer accepted, pre-joining", "action_text": "Call to confirm joining",
     "anchor": "offer_accepted", "start_day": 1,
     "end_day": None, "frequency_type": "every_n_days", "frequency_value": {"n": 2}},
    {"name": "DOJ to Day 7", "action_text": "First-week call",
     "anchor": "doj", "start_day": 1, "end_day": 7,
     "frequency_type": "specific_days", "frequency_value": {"days": [1, 3, 7]}},
    {"name": "Days 8–30", "action_text": "Weekly check-in call",
     "anchor": "doj", "start_day": 8, "end_day": 30,
     "frequency_type": "weekly", "frequency_value": {}},
    {"name": "Days 31–60", "action_text": "Weekly check-in call",
     "anchor": "doj", "start_day": 31, "end_day": 60,
     "frequency_type": "weekly", "frequency_value": {}},
    {"name": "Days 61–75", "action_text": "Twice-weekly check-in",
     "anchor": "doj", "start_day": 61, "end_day": 75,
     "frequency_type": "weekdays", "frequency_value": {"weekdays": ["MON", "THU"]}},
    {"name": "Days 76–90", "action_text": "Close follow-up call",
     "anchor": "doj", "start_day": 76, "end_day": 90,
     "frequency_type": "every_n_days", "frequency_value": {"n": 2}},
    {"name": "Cooling completed", "action_text": "Final status validation",
     "anchor": "cooling_end", "start_day": 0, "end_day": 0,
     "frequency_type": "once", "frequency_value": {}, "template_purpose": _FINAL},
]

DEFAULT_DIGEST_RULES: List[Dict] = [
    {"name": "Team leads – daily", "audience_type": "team_managers", "audience_value": {},
     "scope": "full_subtree", "frequency_type": "daily", "frequency_value": {},
     "send_time": "09:30", "priority": 10},
    {"name": "Senior managers – daily", "audience_type": "has_subordinate_managers",
     "audience_value": {}, "scope": "full_subtree", "frequency_type": "daily",
     "frequency_value": {}, "send_time": "09:30", "priority": 20},
    {"name": "Admins – daily", "audience_type": "admins", "audience_value": {},
     "scope": "organisation", "frequency_type": "daily", "frequency_value": {},
     "send_time": "09:30", "priority": 30},
]


def _sample_values(body: str) -> Dict[str, str]:
    return {k: V.sample_for(k) or k for k in ts.extract_keys(body)}


def ensure_default_templates(db: Session, organization_id: int,
                             user_id: Optional[int]) -> Dict[str, int]:
    """Create any missing seed template (by name). Returns purpose → template id
    (preferring an approved template of that purpose if the org has one)."""
    from app.whatsapp_layer import accounts as acct_svc
    from app.whatsapp_layer.models import WhatsAppTemplate as T

    account = acct_svc.get_any_account(db, organization_id)
    by_purpose: Dict[str, int] = {}
    for spec in DEFAULT_TEMPLATES:
        purpose = spec["purpose"]
        approved = (db.query(T).filter(T.organization_id == organization_id, T.purpose == purpose,
                                       T.approval_status == "approved", T.deleted_at.is_(None),
                                       T.is_active == 1)
                    .order_by(T.version.desc(), T.id.desc()).first())
        existing = (db.query(T).filter(T.organization_id == organization_id,
                                       T.name == spec["name"], T.language == "en_US",
                                       T.deleted_at.is_(None)).first())
        if existing is None:
            existing = T(organization_id=organization_id,
                         account_id=account.id if account else None,
                         language="en_US", category="UTILITY", approval_status="draft",
                         is_active=1, version=1, base_name=spec["name"],
                         created_by=user_id, updated_by=user_id)
            ts.apply_fields(existing, {
                "name": spec["name"], "purpose": purpose,
                "header_format": "TEXT" if spec.get("header_text") else "NONE",
                "header_text": spec.get("header_text"), "body_text": spec["body_text"], "footer_text": None,
                "buttons": spec.get("buttons") or [], "sample_values": _sample_values(spec["body_text"]),
            })
            db.add(existing)
            db.flush()
        by_purpose[purpose] = (approved or existing).id
    return by_purpose


def seed_defaults(db: Session, organization_id: int, user_id: Optional[int],
                  replace: bool = False):
    """Seed §4 rules + digest rules (+ templates). `replace` soft-deletes the
    org's current rules first (reset-defaults). Caller commits."""
    from app.whatsapp_layer.models import (
        ClawbackDigestRule as D, ClawbackNotificationRule as R,
    )
    now = datetime.utcnow()
    tpl = ensure_default_templates(db, organization_id, user_id)
    if replace:
        for model in (R, D):
            (db.query(model).filter(model.organization_id == organization_id,
                                    model.deleted_at.is_(None))
             .update({model.deleted_at: now, model.is_active: 0, model.updated_by: user_id},
                     synchronize_session=False))
    rules, digests = [], []
    for spec in DEFAULT_RULES:
        purpose = spec.get("template_purpose", "recruiter_reminder")
        r = R(organization_id=organization_id, name=spec["name"], stage_label=spec["name"],
              action_text=spec.get("action_text"),
              anchor=spec["anchor"], start_day=spec["start_day"], end_day=spec["end_day"],
              frequency_type=spec["frequency_type"], frequency_value=spec["frequency_value"],
              send_time="10:00", recipient_type="recruiter", template_id=tpl.get(purpose),
              creates_health_check=1, priority=0, is_active=1,
              created_by=user_id, updated_by=user_id)
        db.add(r)
        rules.append(r)
    for spec in DEFAULT_DIGEST_RULES:
        d = D(organization_id=organization_id, name=spec["name"],
              audience_type=spec["audience_type"], audience_value=spec["audience_value"],
              scope=spec["scope"], frequency_type=spec["frequency_type"],
              frequency_value=spec["frequency_value"], send_time=spec["send_time"],
              template_id=tpl.get("team_digest"), sections=list(
                  ("active", "due", "done", "missed", "at_risk", "ending_soon", "final_pending")),
              skip_if_empty=1, priority=spec["priority"], is_active=1,
              created_by=user_id, updated_by=user_id)
        db.add(d)
        digests.append(d)
    db.flush()
    logger.info("Seeded clawback defaults for org %s (replace=%s): %d rules, %d digest rules",
                organization_id, replace, len(rules), len(digests))
    return rules, digests
