"""One path for WhatsApp template status / quality / category changes.

Both the webhook (Meta events forwarded by Omnichannel) and the 15-minute
polling sync call `apply_change`. It updates the template, commits, and then
notifies the person who submitted it plus the org admins (in-app + live
push). A notification is only raised when the stored value actually changes,
and is deduplicated per template + value in Redis, so a webhook and a sync
that see the same change at the same time notify once. Moving to another
value releases the old keys, so approved → paused → approved notifies again.

Meta events (message_template_status_update) → our approval_status
(the DB column is ENUM draft|pending|approved|rejected|paused|disabled):
  APPROVED, REINSTATED, FLAGGED → approved (FLAGGED = still sendable, low quality)
  PENDING, IN_APPEAL            → pending
  REJECTED                      → rejected (reason kept)
  PAUSED, LIMIT_EXCEEDED        → paused
  DISABLED, PENDING_DELETION, DELETED → disabled
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

DEDUPE_TTL = 7 * 24 * 3600
STATUSES = ("pending", "approved", "rejected", "paused", "disabled")
QUALITY_SCORES = ("GREEN", "YELLOW", "RED", "UNKNOWN")
_QUALITY_RANK = {"GREEN": 0, "UNKNOWN": 0, "YELLOW": 1, "RED": 2}

# Meta rejection reasons → plain words (the raw code is stored).
REASON_TEXT = {
    "ABUSIVE_CONTENT": "it contains content Meta considers abusive",
    "INCORRECT_CATEGORY": "the category does not match the content",
    "INVALID_FORMAT": "the format is invalid (check variables, samples and buttons)",
    "SCAM": "Meta flagged it as a possible scam",
    "TAG_CONTENT_MISMATCH": "the content does not match the category",
    "PROMOTIONAL": "it reads as promotional for a utility template",
}


@dataclass
class TemplateChange:
    """What a webhook or a provider poll says about one template."""
    status: Optional[str] = None             # our approval_status vocabulary
    event: Optional[str] = None              # Meta's word (FLAGGED, REINSTATED, ...)
    reason: Optional[str] = None
    category: Optional[str] = None           # new category
    previous_category: Optional[str] = None
    quality_score: Optional[str] = None
    category_notice: Optional[str] = None    # Meta's advance "correct_category"
    source: str = "webhook"                  # webhook | sync


@dataclass
class Notice:
    kind: str                                # status | quality | category
    value: str
    title: str
    message: str
    event: str                               # system toggle name
    # A quiet change (e.g. back to pending, quality recovered): nobody is
    # told, but the dedupe keys of the other values are released.
    silent: bool = False


def template_link(tpl) -> Optional[str]:
    if tpl.account_id is None or tpl.id is None:
        return None
    return f"/settings/channels/whatsapp/{tpl.account_id}/templates/{tpl.id}"


def reason_text(reason: Optional[str]) -> Optional[str]:
    if not reason or str(reason).strip().upper() == "NONE":
        return None
    return REASON_TEXT.get(str(reason).strip().upper(), str(reason).strip())


def find_template(db: Session, account, provider_template_id: Optional[str],
                  name: Optional[str], language: Optional[str]):
    """The account's template by Meta id, else by name (+ language)."""
    from app.whatsapp_layer.models import WhatsAppTemplate as T
    q = db.query(T).filter(T.organization_id == account.organization_id, T.deleted_at.is_(None),
                           or_(T.account_id == account.id, T.account_id.is_(None)))
    if provider_template_id:
        tpl = q.filter(T.provider_template_id == str(provider_template_id)).first()
        if tpl:
            return tpl
    if name:
        tq = q.filter(T.name == name)
        if language:
            # Meta writes en_US; some payloads use en-US.
            tq = tq.filter(T.language.in_({language, language.replace("-", "_")}))
        return tq.order_by(T.account_id.is_(None), T.id.asc()).first()
    return None


# ---------------------------------------------------------------------------
# Pure: what changed + the words for it
# ---------------------------------------------------------------------------
def _status_notice(tpl, prev: Optional[str], status: str, event: Optional[str]) -> Optional[Notice]:
    name = tpl.name
    from app.whatsapp_layer import events as EV
    if prev in (None, "draft"):
        # Imported / never submitted from here: nothing to tell anyone.
        return None
    if status == "approved":
        if prev == "paused" or event == "REINSTATED":
            return Notice("status", status, "WhatsApp template reinstated",
                          f"WhatsApp template '{name}' is active again and can be sent.",
                          EV.ALERT_TEMPLATE_STATUS)
        return Notice("status", status, "WhatsApp template approved",
                      f"WhatsApp template '{name}' was approved — you can now use it in "
                      f"clawback and campaigns.", EV.ALERT_TEMPLATE_STATUS)
    if status == "rejected":
        why = reason_text(tpl.rejection_reason) or "no reason given"
        return Notice("status", status, "WhatsApp template rejected",
                      f"WhatsApp template '{name}' was rejected: {why}. Edit it and submit "
                      f"it again.", EV.ALERT_TEMPLATE_REJECTED)
    if status == "paused":
        why = reason_text(tpl.rejection_reason)
        return Notice("status", status, "WhatsApp template paused",
                      f"WhatsApp template '{name}' was paused by Meta"
                      f"{f' ({why})' if why else ''} because recipients reported or blocked "
                      f"it. Messages using it will not send until it is reinstated; review "
                      f"the content or use another template meanwhile.",
                      EV.ALERT_TEMPLATE_STATUS)
    if status == "disabled":
        why = reason_text(tpl.rejection_reason)
        return Notice("status", status, "WhatsApp template disabled",
                      f"WhatsApp template '{name}' was disabled by Meta"
                      f"{f' ({why})' if why else ''} and can no longer be sent. Create a new "
                      f"version and submit it for approval.", EV.ALERT_TEMPLATE_STATUS)
    return None   # pending: the submitter just did that


def record(tpl, change: TemplateChange) -> Tuple[bool, List[Notice]]:
    """Apply `change` to `tpl` (no DB). Returns (changed, notices)."""
    from app.whatsapp_layer import events as EV
    from app.whatsapp_layer.templates_service import CATEGORIES
    changed, notices = False, []
    name = tpl.name
    was_draft = tpl.approval_status in (None, "draft")

    status = change.status if change.status in STATUSES else None
    if status and status != tpl.approval_status:
        prev = tpl.approval_status
        tpl.approval_status = status
        if status in ("rejected", "paused", "disabled"):
            tpl.rejection_reason = change.reason or (tpl.rejection_reason if status == "rejected"
                                                     else None)
        elif status == "approved":
            tpl.rejection_reason = None
        changed = True
        notices.append(_status_notice(tpl, prev, status, change.event)
                       or Notice("status", status, "", "", "", silent=True))
    elif change.reason is not None and change.source == "sync" and status \
            and status != "approved" and change.reason != tpl.rejection_reason:
        tpl.rejection_reason = change.reason
        changed = True

    if change.event == "FLAGGED" and tpl.approval_status == "approved" \
            and not change.quality_score:
        change.quality_score = "RED"

    score = (change.quality_score or "").upper() or None
    if score in QUALITY_SCORES and score != getattr(tpl, "quality_score", None):
        prev_score = getattr(tpl, "quality_score", None)
        tpl.quality_score = score
        changed = True
        if _QUALITY_RANK.get(score, 0) > _QUALITY_RANK.get(prev_score or "GREEN", 0):
            if score == "RED":
                msg = (f"WhatsApp template '{name}' has low quality (red): recipients are "
                       f"blocking or reporting it. Meta may pause or disable it — review the "
                       f"content and who it is sent to.")
            else:
                msg = (f"WhatsApp template '{name}' quality dropped to medium (yellow). Keep "
                       f"an eye on blocks and reports.")
            notices.append(Notice("quality", score, "WhatsApp template quality dropped", msg,
                                  EV.ALERT_TEMPLATE_STATUS))
        else:
            notices.append(Notice("quality", score, "", "", "", silent=True))

    new_cat = (change.category or "").upper() or None
    if new_cat in CATEGORIES and new_cat != tpl.category:
        old = tpl.category
        tpl.previous_category = (change.previous_category or "").upper() or old
        tpl.category = new_cat
        changed = True
        if not was_draft:
            notices.append(Notice(
                "category", new_cat, "WhatsApp template re-categorised",
                f"Meta changed WhatsApp template '{name}' from {tpl.previous_category} to "
                f"{new_cat}. Pricing and opt-in rules for {new_cat} now apply.",
                EV.ALERT_TEMPLATE_STATUS))
    elif change.previous_category and change.previous_category != tpl.previous_category \
            and change.source == "sync":
        tpl.previous_category = change.previous_category
        changed = True

    notice_cat = (change.category_notice or "").upper() or None
    if notice_cat in CATEGORIES and notice_cat != tpl.category:
        notices.append(Notice(
            "category_notice", notice_cat, "WhatsApp template will be re-categorised",
            f"Meta will change WhatsApp template '{name}' from {tpl.category} to "
            f"{notice_cat}.", EV.ALERT_TEMPLATE_STATUS))

    if changed or change.source == "sync":
        tpl.last_synced_at = datetime.utcnow()
    return changed, notices


# ---------------------------------------------------------------------------
# DB glue
# ---------------------------------------------------------------------------
_KEY_PREFIX = {"status": "tpl_status", "quality": "tpl_quality", "category": "tpl_category",
               "category_notice": "tpl_category_notice"}
_KIND_VALUES = {"status": STATUSES, "quality": QUALITY_SCORES}


def _dedupe_key(tpl, kind: str, value: str) -> str:
    return f"{_KEY_PREFIX[kind]}:{tpl.id}:{value}"


def send(db: Session, tpl, notices: List[Notice]) -> List[int]:
    """Raise each notice once (dedupe per template + value). Returns the ids
    of the notifications created."""
    from app.whatsapp_layer import alerts
    out = []
    for n in notices:
        others = [_dedupe_key(tpl, n.kind, v) for v in _KIND_VALUES.get(n.kind, ())
                  if v != n.value]
        alerts.release(tpl.organization_id, others)
        if n.silent:
            continue
        if not alerts.claim(tpl.organization_id, _dedupe_key(tpl, n.kind, n.value), DEDUPE_TTL):
            logger.info("Template %s %s=%s already notified", tpl.id, n.kind, n.value)
            continue
        meta: Dict[str, object] = {"template_id": tpl.id, "template_name": tpl.name,
                                   "account_id": tpl.account_id, "kind": f"template_{n.kind}",
                                   "value": n.value}
        link = template_link(tpl)
        if link:
            meta["link"] = link
        nid = alerts.notify_org_admins(
            db, tpl.organization_id, title=n.title, message=n.message, metadata=meta,
            event=n.event, user_ids=[tpl.updated_by if tpl.submitted_at else None,
                                     tpl.created_by])
        if nid:
            out.append(nid)
    return out


def apply_change(db: Session, tpl, change: TemplateChange) -> bool:
    """Apply a webhook / sync change to a stored template, commit, notify.
    Returns True when the template changed."""
    changed, notices = record(tpl, change)
    if changed:
        logger.info("Template %s (%s) ← %s: status=%s quality=%s category=%s",
                    tpl.id, tpl.name, change.source, tpl.approval_status,
                    getattr(tpl, "quality_score", None), tpl.category)
    if changed or notices:
        db.commit()
    if notices:
        send(db, tpl, notices)
    return changed
