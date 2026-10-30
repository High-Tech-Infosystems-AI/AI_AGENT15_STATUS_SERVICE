"""WhatsApp templates: named ↔ positional conversion, validation, preview,
submit to the provider and approval sync.

Admins write `Hi {{recruiter_name}}, ...`; the provider (Meta) only accepts
positional `{{1}}, {{2}}`. On save we store both, plus `variable_map`
({"header": [...], "body": [...]}, index + 1 = position) which the sender
uses to turn a params dict into the ordered variable lists.

The top half of this module is pure (unit-tested); the DB glue follows.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.whatsapp_layer import variables as V

logger = logging.getLogger("app_logger")

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_ADJACENT_RE = re.compile(r"\}\}\s*\{\{")
NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(_[A-Za-z]{2,4})?$")

BODY_MAX = 1024
HEADER_MAX = 60
FOOTER_MAX = 60
BUTTON_LABEL_MAX = 25
MAX_BUTTONS = 3

CATEGORIES = ("UTILITY", "MARKETING")
HEADER_FORMATS = ("NONE", "TEXT")
BUTTON_TYPES = ("URL", "QUICK_REPLY")
APPROVAL_STATUSES = ("draft", "pending", "approved", "rejected", "paused", "disabled")
EDITABLE_STATUSES = ("draft", "rejected")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def extract_keys(text: Optional[str]) -> List[str]:
    """Placeholder keys in order of first appearance (no duplicates)."""
    seen: List[str] = []
    for m in PLACEHOLDER_RE.finditer(text or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def to_positional(text: Optional[str]) -> Tuple[str, List[str]]:
    """`Hi {{a}}, {{b}} and {{a}}` → (`Hi {{1}}, {{2}} and {{1}}`, ["a", "b"]).
    A key used twice keeps the same position."""
    keys = extract_keys(text)
    index = {k: i + 1 for i, k in enumerate(keys)}
    out = PLACEHOLDER_RE.sub(lambda m: "{{%d}}" % index[m.group(1)], text or "")
    return out, keys


def to_named(text: Optional[str], keys: List[str]) -> str:
    """Inverse of `to_positional`: `{{1}}` → `{{keys[0]}}`. Unknown
    positions are left untouched."""
    def _sub(m):
        token = m.group(1)
        if token.isdigit() and 1 <= int(token) <= len(keys):
            return "{{%s}}" % keys[int(token) - 1]
        return m.group(0)
    return PLACEHOLDER_RE.sub(_sub, text or "")


def clean_param(value: Any) -> str:
    """Meta rejects variables with newlines, tabs or 4+ consecutive spaces,
    and empty values."""
    s = "" if value is None else str(value)
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r" {4,}", "   ", s).strip()
    return s or "-"


def _err(field: str, message: str) -> Dict[str, str]:
    return {"field": field, "message": message}


def validate_template(data: Dict[str, Any], *, for_submit: bool = False) -> List[Dict[str, str]]:
    """Contract §2.2 validation. Returns a list of {field, message}; empty = valid.

    `data` keys: name, language, category, purpose, header_format,
    header_text, body_text, footer_text, buttons, sample_values.
    Sample values are only required when `for_submit`."""
    errors: List[Dict[str, str]] = []
    name = data.get("name") or ""
    purpose = data.get("purpose") or ""
    header_format = (data.get("header_format") or "NONE").upper()
    header_text = data.get("header_text") or ""
    body = data.get("body_text") or ""
    footer = data.get("footer_text") or ""
    buttons = data.get("buttons") or []
    samples = data.get("sample_values") or {}

    if not NAME_RE.match(name):
        errors.append(_err("name", "Use 1–512 lowercase letters, digits or underscores"))
    if not LANGUAGE_RE.match(data.get("language") or ""):
        errors.append(_err("language", "Use a WhatsApp language code such as en_US"))
    if (data.get("category") or "") not in CATEGORIES:
        errors.append(_err("category", f"Must be one of {', '.join(CATEGORIES)}"))
    if purpose not in V.PURPOSES:
        errors.append(_err("purpose", f"Must be one of {', '.join(V.PURPOSES)}"))
    if header_format not in HEADER_FORMATS:
        errors.append(_err("header_format", "Must be NONE or TEXT"))

    allowed = set(V.allowed_keys(purpose)) if purpose in V.PURPOSES else set()

    def _check_keys(field: str, keys: List[str]) -> None:
        for k in keys:
            if k not in V.VARIABLES_BY_KEY:
                errors.append(_err(field, f"Unknown variable '{{{{{k}}}}}'"))
            elif purpose in V.PURPOSES and k not in allowed:
                errors.append(_err(field, f"Variable '{{{{{k}}}}}' is not allowed for purpose '{purpose}'"))

    # Header
    header_keys: List[str] = []
    if header_format == "TEXT":
        if not header_text.strip():
            errors.append(_err("header_text", "Header text is required when the header format is TEXT"))
        else:
            header_pos, header_keys = to_positional(header_text)
            if len(header_pos) > HEADER_MAX:
                errors.append(_err("header_text", f"Header must be at most {HEADER_MAX} characters"))
            if len(PLACEHOLDER_RE.findall(header_text)) > 1:
                errors.append(_err("header_text", "Header may contain at most 1 variable"))
            _check_keys("header_text", header_keys)

    # Body
    body_pos, body_keys = to_positional(body)
    if not body.strip():
        errors.append(_err("body_text", "Body is required"))
    elif len(body_pos) > BODY_MAX:
        errors.append(_err("body_text", f"Body must be at most {BODY_MAX} characters"))
    stripped = body.strip()
    if stripped:
        if PLACEHOLDER_RE.match(stripped):
            errors.append(_err("body_text", "Body must not start with a variable"))
        if re.search(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}$", stripped):
            errors.append(_err("body_text", "Body must not end with a variable"))
    if _ADJACENT_RE.search(body):
        errors.append(_err("body_text", "Two variables must not be next to each other"))
    _check_keys("body_text", body_keys)

    # Footer
    if footer:
        if len(footer) > FOOTER_MAX:
            errors.append(_err("footer_text", f"Footer must be at most {FOOTER_MAX} characters"))
        if PLACEHOLDER_RE.search(footer):
            errors.append(_err("footer_text", "Footer must not contain variables"))

    # Buttons
    if not isinstance(buttons, list):
        errors.append(_err("buttons", "Buttons must be a list"))
        buttons = []
    if len(buttons) > MAX_BUTTONS:
        errors.append(_err("buttons", f"At most {MAX_BUTTONS} buttons"))
    for i, b in enumerate(buttons):
        b = b if isinstance(b, dict) else {}
        btype = (b.get("type") or "").upper()
        label = (b.get("label") or "").strip()
        if btype not in BUTTON_TYPES:
            errors.append(_err(f"buttons[{i}].type", "Must be URL or QUICK_REPLY"))
        if not label:
            errors.append(_err(f"buttons[{i}].label", "Label is required"))
        elif len(label) > BUTTON_LABEL_MAX:
            errors.append(_err(f"buttons[{i}].label", f"Label must be at most {BUTTON_LABEL_MAX} characters"))
        if btype == "URL" and not re.match(r"^https?://", b.get("url") or ""):
            errors.append(_err(f"buttons[{i}].url", "A URL button needs an http(s) URL"))

    # Sample values (submit only)
    if for_submit:
        for k in header_keys + [k for k in body_keys if k not in header_keys]:
            if not str(samples.get(k) or "").strip():
                errors.append(_err("sample_values", f"Sample value required for '{{{{{k}}}}}'"))
    return errors


def build_variable_map(header_format: str, header_text: Optional[str],
                       body_text: str) -> Dict[str, List[str]]:
    header_keys = extract_keys(header_text) if (header_format or "NONE") == "TEXT" else []
    return {"header": header_keys, "body": extract_keys(body_text)}


def send_variables(variable_map: Optional[Dict[str, List[str]]],
                   params: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Ordered (header_vars, body_vars) for the provider from a params dict."""
    vm = variable_map or {}
    header = [clean_param(params.get(k)) for k in (vm.get("header") or [])]
    body = [clean_param(params.get(k)) for k in (vm.get("body") or [])]
    return header, body


def _fill(text: Optional[str], values: Dict[str, Any]) -> Optional[str]:
    if text is None:
        return None

    def _sub(m):
        key = m.group(1)
        if key in values and values[key] not in (None, ""):
            return str(values[key])
        return m.group(0)
    return PLACEHOLDER_RE.sub(_sub, text)


def render_preview(header_format: str, header_text: Optional[str], body_text: str,
                   footer_text: Optional[str], buttons: Optional[List[Dict]],
                   values: Optional[Dict[str, Any]] = None,
                   sample_values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Render with `values`, falling back to `sample_values` per key."""
    merged: Dict[str, Any] = dict(sample_values or {})
    merged.update({k: v for k, v in (values or {}).items() if v not in (None, "")})
    out_buttons = []
    for b in buttons or []:
        nb = dict(b)
        if nb.get("url"):
            nb["url"] = _fill(nb["url"], merged)
        out_buttons.append(nb)
    return {
        "header": _fill(header_text, merged) if (header_format or "NONE") == "TEXT" and header_text else None,
        "body": _fill(body_text, merged) or "",
        "footer": footer_text or None,
        "buttons": out_buttons,
    }


def render_body(tpl, params: Dict[str, Any]) -> str:
    """The message body as the recipient will read it (stored on the log)."""
    return _fill(tpl.body_text_named, {k: clean_param(v) for k, v in (params or {}).items()}) or ""


def next_version_name(base_name: str, version: int) -> str:
    return f"{base_name}_v{version}"


def base_name_of(name: str) -> str:
    m = re.match(r"^(.*)_v\d+$", name or "")
    return m.group(1) if m else name


# ---------------------------------------------------------------------------
# DB glue
# ---------------------------------------------------------------------------
def template_data(tpl) -> Dict[str, Any]:
    """Model → the dict shape `validate_template` takes."""
    return {
        "name": tpl.name, "language": tpl.language, "category": tpl.category,
        "purpose": tpl.purpose, "header_format": tpl.header_format,
        "header_text": tpl.header_text, "body_text": tpl.body_text_named,
        "footer_text": tpl.footer_text, "buttons": tpl.buttons or [],
        "sample_values": tpl.sample_values or {},
    }


def apply_fields(tpl, data: Dict[str, Any]) -> None:
    """Copy editor fields onto the model and recompute the derived columns."""
    for field in ("name", "language", "category", "purpose", "header_format",
                  "header_text", "footer_text", "buttons", "sample_values"):
        if field in data:
            setattr(tpl, field, data[field])
    if "body_text" in data:
        tpl.body_text_named = data["body_text"]
    tpl.header_format = (tpl.header_format or "NONE").upper()
    if tpl.header_format != "TEXT":
        tpl.header_text = None
    tpl.body_text_positional = to_positional(tpl.body_text_named)[0]
    tpl.variable_map = build_variable_map(tpl.header_format, tpl.header_text,
                                          tpl.body_text_named)


def in_use_counts(db: Session, template_ids: List[int]) -> Dict[int, int]:
    """Active (non-deleted) rules + digest rules referencing each template."""
    from app.whatsapp_layer.models import ClawbackDigestRule, ClawbackNotificationRule
    counts: Dict[int, int] = {tid: 0 for tid in template_ids}
    if not template_ids:
        return counts
    for model in (ClawbackNotificationRule, ClawbackDigestRule):
        rows = (db.query(model.template_id, func.count(model.id))
                .filter(model.template_id.in_(template_ids), model.deleted_at.is_(None))
                .group_by(model.template_id).all())
        for tid, n in rows:
            counts[tid] = counts.get(tid, 0) + int(n)
    return counts


def serialize(tpl, in_use: int = 0) -> Dict[str, Any]:
    vm = tpl.variable_map or {}
    keys = list(vm.get("header") or []) + [k for k in (vm.get("body") or [])
                                          if k not in (vm.get("header") or [])]
    return {
        "id": tpl.id,
        "organization_id": tpl.organization_id,
        "account_id": tpl.account_id,
        "name": tpl.name,
        "language": tpl.language,
        "category": tpl.category,
        "purpose": tpl.purpose,
        "header_format": tpl.header_format,
        "header_text": tpl.header_text,
        "body_text": tpl.body_text_named,
        "body_text_positional": tpl.body_text_positional,
        "footer_text": tpl.footer_text,
        "buttons": tpl.buttons or [],
        "variables": keys,
        "sample_values": tpl.sample_values or {},
        "provider_template_id": tpl.provider_template_id,
        "approval_status": tpl.approval_status,
        "rejection_reason": tpl.rejection_reason,
        "previous_category": tpl.previous_category,
        "last_synced_at": _iso(tpl.last_synced_at),
        "is_active": bool(tpl.is_active),
        "version": tpl.version,
        "in_use_by_rules": in_use,
        "created_at": _iso(tpl.created_at),
        "updated_at": _iso(tpl.updated_at),
    }


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.replace(microsecond=0).isoformat() + "Z"
    return str(dt)


def name_taken(db: Session, organization_id: int, account_id: Optional[int],
               name: str, language: str, exclude_id: Optional[int] = None) -> bool:
    """Name is unique per account + language among non-deleted templates
    (drafts without an account count against the org)."""
    from app.whatsapp_layer.models import WhatsAppTemplate as T
    q = db.query(T.id).filter(T.organization_id == organization_id, T.name == name,
                              T.language == language, T.deleted_at.is_(None))
    if account_id is not None:
        q = q.filter(or_(T.account_id == account_id, T.account_id.is_(None)))
    if exclude_id is not None:
        q = q.filter(T.id != exclude_id)
    return db.query(q.exists()).scalar()


def build_spec(tpl):
    from app.whatsapp_layer.providers.base import TemplateSpec
    vm = tpl.variable_map or build_variable_map(tpl.header_format, tpl.header_text,
                                                tpl.body_text_named)
    samples = tpl.sample_values or {}
    header_pos = to_positional(tpl.header_text)[0] if tpl.header_format == "TEXT" else None
    return TemplateSpec(
        name=tpl.name,
        language=tpl.language,
        category=tpl.category,
        header_format=tpl.header_format or "NONE",
        header_text=header_pos,
        header_example=[clean_param(samples.get(k)) for k in (vm.get("header") or [])],
        body_text=tpl.body_text_positional or to_positional(tpl.body_text_named)[0],
        body_example=[clean_param(samples.get(k)) for k in (vm.get("body") or [])],
        footer_text=tpl.footer_text,
        buttons=list(tpl.buttons or []),
    )


def submit(db: Session, tpl, account, user_id: Optional[int]) -> None:
    """Send a draft/rejected template to the provider; becomes `pending`.
    Raises ProviderError on failure (the API maps it to 502/409)."""
    from app.whatsapp_layer import accounts as acct_svc
    from app.whatsapp_layer.providers.registry import get_provider
    apply_fields(tpl, {})  # make sure positional + variable_map are current
    provider = get_provider(account.provider)
    result = provider.create_template(acct_svc.credentials(account), build_spec(tpl))
    tpl.account_id = account.id
    tpl.provider_template_id = result.provider_template_id or tpl.provider_template_id
    tpl.approval_status = result.status or "pending"
    tpl.rejection_reason = result.rejection_reason
    tpl.previous_category = result.previous_category
    tpl.submitted_at = datetime.utcnow()
    tpl.updated_by = user_id
    logger.info("WhatsApp template %s (%s) submitted for org %s → %s",
                tpl.id, tpl.name, tpl.organization_id, tpl.approval_status)


def apply_provider_status(tpl, ptpl) -> bool:
    """Copy a provider status onto a template; returns True when changed."""
    changed = False
    if ptpl.status and ptpl.status != tpl.approval_status:
        tpl.approval_status = ptpl.status
        changed = True
    if ptpl.rejection_reason is not None and ptpl.rejection_reason != tpl.rejection_reason:
        tpl.rejection_reason = ptpl.rejection_reason
        changed = True
    if ptpl.previous_category and ptpl.previous_category != tpl.previous_category:
        tpl.previous_category = ptpl.previous_category
        changed = True
    if ptpl.category and ptpl.category in CATEGORIES and ptpl.category != tpl.category:
        # Meta re-categorised it (e.g. UTILITY → MARKETING); keep the old one visible.
        tpl.previous_category = tpl.previous_category or tpl.category
        tpl.category = ptpl.category
        changed = True
    if ptpl.provider_template_id and not tpl.provider_template_id:
        tpl.provider_template_id = ptpl.provider_template_id
        changed = True
    tpl.last_synced_at = datetime.utcnow()
    return changed


def clean_provider_body(text: Optional[str]) -> str:
    """BroadNet returns CRLF line ends and a trailing line break."""
    return (text or "").replace("\r\n", "\n").strip()


def purpose_for_import(name: str, body_text: str) -> str:
    """Which messages an imported template can carry, from its variables."""
    keys = set(extract_keys(body_text))
    if "candidate_list" in keys:
        return "team_digest" if "recipient_name" in keys else "recruiter_batch"
    if "missed_count" in keys or "recipient_name" in keys:
        return "team_digest"
    if "candidate_name" in keys:
        return "final_validation" if "final_validation" in name else "recruiter_reminder"
    return "generic"


def copy_provider_content(tpl, ptpl) -> None:
    """Make a template match what the provider holds, so the variables we
    send line up with the approved body."""
    tpl.purpose = purpose_for_import(tpl.name, clean_provider_body(ptpl.body_text))
    apply_fields(tpl, {
        "header_format": "TEXT" if ptpl.header_text else "NONE",
        "header_text": ptpl.header_text,
        "body_text": clean_provider_body(ptpl.body_text),
        "footer_text": ptpl.footer_text,
        "sample_values": ptpl.sample_values or None,
    })


def import_from_provider(db: Session, account) -> Dict[str, int]:
    """Bring the account's provider templates into HRMIS: add the missing ones,
    refresh the content of local drafts with the same name, update statuses."""
    from app.whatsapp_layer import accounts as acct_svc
    from app.whatsapp_layer.models import WhatsAppTemplate as T
    from app.whatsapp_layer.providers.registry import get_provider

    imported = updated = 0
    for ptpl in get_provider(account.provider).list_templates(acct_svc.credentials(account)):
        if not ptpl.name or not ptpl.body_text:
            continue
        language = ptpl.language or "en_US"
        tpl = (db.query(T).filter(T.organization_id == account.organization_id, T.name == ptpl.name,
                                  T.language == language, T.deleted_at.is_(None)).first())
        if tpl is None:
            if ptpl.category not in CATEGORIES:
                logger.info("Skipping provider template %s: category %s", ptpl.name, ptpl.category)
                continue
            tpl = T(organization_id=account.organization_id, account_id=account.id, name=ptpl.name,
                    language=language, category=ptpl.category, approval_status="pending",
                    provider_template_id=ptpl.provider_template_id)
            copy_provider_content(tpl, ptpl)
            db.add(tpl)
            apply_provider_status(tpl, ptpl)
            imported += 1
            logger.info("Imported template %s for org %s (%s)", tpl.name,
                        account.organization_id, tpl.approval_status)
            continue
        changed = False
        if tpl.approval_status == "draft":
            copy_provider_content(tpl, ptpl)
            changed = True
        tpl.account_id = tpl.account_id or account.id
        if tpl.approval_status != "disabled" and apply_provider_status(tpl, ptpl):
            changed = True
        updated += 1 if changed else 0
    return {"imported": imported, "updated": updated}


def sync_templates(db: Session, organization_id: Optional[int] = None) -> Dict[str, int]:
    """Import the provider's templates, then poll any still `pending`. Providers
    without a template read API are skipped quietly."""
    from app.whatsapp_layer import accounts as acct_svc
    from app.whatsapp_layer.models import WhatsAppProviderAccount as A, WhatsAppTemplate as T
    from app.whatsapp_layer.providers.base import ProviderError, ProviderUnsupported
    from app.whatsapp_layer.providers.registry import get_provider

    q = db.query(T).filter(T.approval_status == "pending", T.deleted_at.is_(None),
                           T.account_id.isnot(None))
    if organization_id is not None:
        q = q.filter(T.organization_id == organization_id)
    checked = updated = imported = failed = 0
    accounts = db.query(A).filter(A.status == "active")
    if organization_id is not None:
        accounts = accounts.filter(A.organization_id == organization_id)
    for account in accounts.all():
        try:
            res = import_from_provider(db, account)
            imported += res["imported"]
            updated += res["updated"]
            db.commit()
        except ProviderUnsupported:
            db.rollback()
            logger.info("Template list API unavailable for account %s", account.id)
        except ProviderError as exc:
            db.rollback()
            failed += 1
            logger.warning("Template import failed for account %s: %s", account.id, exc.message)
            if exc.auth_error:
                acct_svc.mark_auth_error(db, account, exc.message)
        except Exception as exc:
            db.rollback()
            failed += 1
            logger.warning("Template import failed for account %s: %s", account.id, exc)
    unsupported_accounts = set()
    for tpl in q.all():
        if tpl.account_id in unsupported_accounts:
            continue
        account = db.query(A).filter(A.id == tpl.account_id).first()
        if not account or account.status != "active":
            continue
        checked += 1
        try:
            ptpl = get_provider(account.provider).get_template(
                acct_svc.credentials(account), tpl.name, tpl.language)
        except ProviderUnsupported:
            unsupported_accounts.add(account.id)
            logger.info("Template status API unavailable for account %s; skipping sync", account.id)
            continue
        except ProviderError as exc:
            logger.warning("Template sync failed for template %s: %s", tpl.id, exc.message)
            if exc.auth_error:
                acct_svc.mark_auth_error(db, account, exc.message)
            continue
        except Exception as exc:  # decrypt errors etc.
            logger.warning("Template sync failed for template %s: %s", tpl.id, exc)
            continue
        if ptpl and apply_provider_status(tpl, ptpl):
            updated += 1
            logger.info("Template %s (%s) is now %s", tpl.id, tpl.name, tpl.approval_status)
    db.commit()
    return {"checked": checked, "updated": updated, "imported": imported, "failed": failed}
