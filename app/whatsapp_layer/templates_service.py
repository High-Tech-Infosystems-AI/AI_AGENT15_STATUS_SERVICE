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

from app.whatsapp_layer import template_rules as R
from app.whatsapp_layer.template_rules import (  # noqa: F401  (re-exported)
    BODY_MAX, BUTTON_LABEL_MAX, CATEGORIES, FOOTER_MAX, HEADER_FORMATS, HEADER_MAX,
    LANGUAGE_RE, MAX_BUTTONS, MEDIA_HEADERS, NAME_ERROR, NAME_RE, PLACEHOLDER_RE,
    extract_keys, normalise, template_warnings, to_positional, validate_template,
)

logger = logging.getLogger("app_logger")

APPROVAL_STATUSES = ("draft", "pending", "approved", "rejected", "paused", "disabled")
EDITABLE_STATUSES = ("draft", "rejected")

# Editor fields stored as-is on the model (body_text maps to body_text_named).
EDITOR_FIELDS = ("name", "language", "category", "template_type", "purpose", "variable_type",
                 "header_format", "header_text", "header_media", "header_sample_values",
                 "footer_text", "buttons", "sample_values", "limited_time_offer",
                 "carousel_cards", "auth_config", "custom_validity_period",
                 "validity_period_seconds")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
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


def build_variable_map(header_format: str, header_text: Optional[str],
                       body_text: str, variable_type: Optional[str] = None) -> Dict[str, List[str]]:
    """{"header": [...], "body": [...]}, index + 1 = position. Numbered
    templates keep their own numbers ({{1}}, {{2}}, ...)."""
    header_keys = extract_keys(header_text) if (header_format or "NONE") == "TEXT" else []
    if (variable_type or "").upper() == "NUMBER":
        return {"header": R.numbered_keys(header_text) if header_keys else [],
                "body": R.numbered_keys(body_text)}
    return {"header": header_keys, "body": extract_keys(body_text)}


def send_variables(variable_map: Optional[Dict[str, List[str]]],
                   params: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Ordered (header_vars, body_vars) for the provider from a params dict.
    `header_<key>` overrides a header value (numbered templates use {{1}}
    in both the header and the body)."""
    vm = variable_map or {}
    header = [clean_param(params.get(f"header_{k}", params.get(k))) for k in (vm.get("header") or [])]
    body = [clean_param(params.get(k)) for k in (vm.get("body") or [])]
    return header, body


def body_param_names(tpl) -> List[str]:
    """Variable names to send with each body value; empty for numbered templates."""
    if (tpl.parameter_format or "").upper() != "NAMED":
        return []
    return list((tpl.variable_map or {}).get("body") or [])


def _fill(text: Optional[str], values: Dict[str, Any]) -> Optional[str]:
    if text is None:
        return None

    def _sub(m):
        key = m.group(1)
        if key in values and values[key] not in (None, ""):
            return str(values[key])
        return m.group(0)
    return PLACEHOLDER_RE.sub(_sub, text)


def _preview_button(b: Dict[str, Any], merged: Dict[str, Any]) -> Dict[str, Any]:
    nb = dict(b)
    if nb.get("url"):
        # A URL variable is the button's own parameter: its sample wins.
        if nb.get("url_example"):
            nb["url"] = PLACEHOLDER_RE.sub(str(nb["url_example"]), nb["url"])
        else:
            nb["url"] = _fill(nb["url"], merged)
    return nb


def render_preview(header_format: str, header_text: Optional[str], body_text: str,
                   footer_text: Optional[str], buttons: Optional[List[Dict]],
                   values: Optional[Dict[str, Any]] = None,
                   sample_values: Optional[Dict[str, Any]] = None, *,
                   header_sample_values: Optional[Dict[str, Any]] = None,
                   header_media: Optional[Dict[str, Any]] = None,
                   limited_time_offer: Optional[Dict[str, Any]] = None,
                   carousel_cards: Optional[List[Dict[str, Any]]] = None,
                   auth_config: Optional[Dict[str, Any]] = None,
                   category: Optional[str] = None) -> Dict[str, Any]:
    """Render with `values`, falling back to `sample_values` per key (an empty
    sample leaves the raw {{n}} in place, as the editor preview does)."""
    merged: Dict[str, Any] = dict(sample_values or {})
    merged.update({k: v for k, v in (values or {}).items() if v not in (None, "")})
    header_fmt = (header_format or "NONE").upper()
    if (category or "").upper() == "AUTHENTICATION":
        cfg = auth_config or {}
        body = "*123456* is your verification code."
        if cfg.get("add_security_recommendation"):
            body += " " + R.AUTH_SECURITY_TEXT
        exp = cfg.get("code_expiration_minutes")
        return {"header_type": "NONE", "header": None, "header_media": None,
                "limited_time_offer": None, "body": body,
                "footer": f"Expires in {exp} minutes." if exp else None,
                "buttons": [{"type": "OTP", "label": cfg.get("button_text") or R.AUTH_DEFAULT_BUTTON,
                             "otp_type": cfg.get("code_delivery")}],
                "carousel_cards": None}
    header_vals = dict(merged)
    header_vals.update({k: v for k, v in (header_sample_values or {}).items() if v not in (None, "")})
    header_vals.update({k[len("header_"):]: v for k, v in (values or {}).items()
                        if k.startswith("header_") and v not in (None, "")})
    cards = None
    if header_fmt == "CAROUSEL":
        cards = []
        for c in carousel_cards or []:
            card_vals = dict(c.get("sample_values") or {})
            card_vals.update({k: v for k, v in (values or {}).items() if v not in (None, "")})
            cards.append({"media": c.get("media"), "body": _fill(c.get("body_text"), card_vals),
                          "buttons": [_preview_button(b, card_vals) for b in c.get("buttons") or []]})
    return {
        "header_type": header_fmt,
        "header": _fill(header_text, header_vals) if header_fmt == "TEXT" and header_text else None,
        "header_media": header_media if header_fmt in MEDIA_HEADERS else None,
        "limited_time_offer": limited_time_offer or None,
        "body": _fill(body_text, merged) or "",
        "footer": footer_text or None,
        "buttons": [_preview_button(b, merged) for b in buttons or []],
        "carousel_cards": cards,
    }


def preview_of(tpl_or_data, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Preview of a saved template (model) or an unsaved editor payload (dict)."""
    d = normalise(tpl_or_data if isinstance(tpl_or_data, dict) else template_data(tpl_or_data))
    return render_preview(d["header_format"], d.get("header_text"), d.get("body_text") or "",
                          d.get("footer_text"), d.get("buttons") or [], values,
                          d.get("sample_values"), header_sample_values=d.get("header_sample_values"),
                          header_media=_media_out(d.get("header_media")),
                          limited_time_offer=d.get("limited_time_offer"),
                          carousel_cards=[{**c, "media": _media_out(c.get("media"))}
                                          for c in d.get("carousel_cards") or []],
                          auth_config=d.get("auth_config"), category=d["category"])


def render_body(tpl, params: Dict[str, Any]) -> str:
    """The message body as the recipient will read it (stored on the log)."""
    return _fill(tpl.body_text_named, {k: clean_param(v) for k, v in (params or {}).items()}) or ""


def next_version_name(base_name: str, version: int) -> str:
    return f"{base_name}_v{version}"


def base_name_of(name: str) -> str:
    m = re.match(r"^(.*)_v\d+$", name or "")
    return m.group(1) if m else name


def button_summary(buttons: Optional[List[Dict[str, Any]]], auth_config=None) -> List[str]:
    """List column "Button Type": `URL: https://...`, `Quick Reply: View`, `otp: Verify`."""
    if auth_config:
        return [f"otp: {auth_config.get('button_text') or R.AUTH_DEFAULT_BUTTON}"]
    out = []
    for b in buttons or []:
        btype = (b.get("type") or "").upper()
        value = b.get("url") if btype == "URL" else b.get("label")
        out.append(f"{R.BUTTON_LABELS.get(btype, btype)}: {value or ''}".strip())
    return out


# ---------------------------------------------------------------------------
# Media (header file / carousel cards)
# ---------------------------------------------------------------------------
def _media_out(media: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Stored media + a short-lived download URL when it lives in S3."""
    if not media:
        return None
    out = dict(media)
    if media.get("s3_key") and not media.get("url"):
        try:
            from app.chat_layer import s3_chat_service as s3
            out["preview_url"] = s3.presign_get(media["s3_key"])
        except Exception:  # S3 not configured
            out["preview_url"] = None
    else:
        out["preview_url"] = media.get("url")
    return out


def media_url(media: Optional[Dict[str, Any]]) -> Optional[str]:
    """A URL the provider can fetch (carousel cards)."""
    if not media:
        return None
    return media.get("url") or (_media_out(media) or {}).get("preview_url")


class MediaError(ValueError):
    pass


def _public_host(host: str) -> bool:
    import ipaddress
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def fetch_media(media: Dict[str, Any], kind: str) -> Tuple[str, bytes, str]:
    """(file_name, bytes, mime) for the provider's `headerfile` part."""
    import requests
    from urllib.parse import urlparse
    limit = R.MEDIA_MAX_BYTES.get(kind, 16 * 1024 * 1024)
    name = media.get("file_name") or "header"
    mime = media.get("mime_type") or (R.MEDIA_MIME.get(kind) or ("application/octet-stream",))[0]
    if media.get("s3_key"):
        from app.chat_layer import s3_chat_service as s3
        from app.core import settings
        obj = s3._client().get_object(Bucket=settings.AWS_S3_BUCKET_CHAT, Key=media["s3_key"])
        data = obj["Body"].read(limit + 1)
    else:
        url = media.get("url") or ""
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or not _public_host(p.hostname):
            raise MediaError("The media URL must be a public http(s) URL")
        resp = requests.get(url, timeout=20, stream=True, allow_redirects=False)
        if resp.status_code >= 400:
            raise MediaError(f"Could not download the media (HTTP {resp.status_code})")
        data = resp.raw.read(limit + 1, decode_content=True)
        mime = media.get("mime_type") or (resp.headers.get("Content-Type") or mime).split(";")[0]
        name = media.get("file_name") or (p.path.rsplit("/", 1)[-1] or name)
    if len(data) > limit:
        raise MediaError(f"{kind.title()} files must be at most {limit // (1024 * 1024)} MB")
    return name, data, mime


# ---------------------------------------------------------------------------
# DB glue
# ---------------------------------------------------------------------------
def template_data(tpl) -> Dict[str, Any]:
    """Model → the dict shape `validate_template` takes."""
    return {
        "name": tpl.name, "language": tpl.language, "category": tpl.category,
        "template_type": tpl.template_type, "purpose": tpl.purpose,
        "variable_type": tpl.variable_type, "header_format": tpl.header_format,
        "header_text": tpl.header_text, "header_media": tpl.header_media,
        "header_sample_values": tpl.header_sample_values or {},
        "body_text": tpl.body_text_named, "footer_text": tpl.footer_text,
        "buttons": tpl.buttons or [], "sample_values": tpl.sample_values or {},
        "limited_time_offer": tpl.limited_time_offer, "carousel_cards": tpl.carousel_cards,
        "auth_config": tpl.auth_config,
        "custom_validity_period": bool(tpl.custom_validity_period),
        "validity_period_seconds": tpl.validity_period_seconds,
    }


def apply_fields(tpl, data: Dict[str, Any]) -> None:
    """Copy editor fields onto the model and recompute the derived columns."""
    merged = template_data(tpl)
    merged.update({k: v for k, v in data.items() if k in EDITOR_FIELDS or k == "body_text"})
    d = normalise(merged)
    for field in EDITOR_FIELDS:
        value = d.get(field)
        if field in ("header_sample_values", "sample_values"):
            value = value or None
        if field == "custom_validity_period":
            value = 1 if value else 0
        setattr(tpl, field, value)
    tpl.body_text_named = d.get("body_text") or ""
    if tpl.variable_type == "NUMBER":
        tpl.body_text_positional = tpl.body_text_named
    else:
        tpl.body_text_positional = to_positional(tpl.body_text_named)[0]
    tpl.variable_map = build_variable_map(tpl.header_format, tpl.header_text,
                                          tpl.body_text_named, tpl.variable_type)


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


def _variable_type(tpl) -> str:
    return tpl.variable_type or R.infer_variable_type(
        tpl.header_text if tpl.header_format == "TEXT" else None, tpl.body_text_named)


def _template_type(tpl) -> str:
    if tpl.template_type:
        return tpl.template_type
    return "OTP" if tpl.category == "AUTHENTICATION" else "DEFAULT"


def time_to_live(tpl) -> int:
    """List column "Time to Live": the custom validity in seconds, else 0."""
    if tpl.custom_validity_period and tpl.validity_period_seconds:
        return int(tpl.validity_period_seconds)
    return 0


def serialize(tpl, in_use: int = 0) -> Dict[str, Any]:
    vm = tpl.variable_map or {}
    keys = list(vm.get("header") or []) + [k for k in (vm.get("body") or [])
                                          if k not in (vm.get("header") or [])]
    data = template_data(tpl)
    template_type = _template_type(tpl)
    return {
        "id": tpl.id,
        "organization_id": tpl.organization_id,
        "account_id": tpl.account_id,
        "name": tpl.name,
        "language": tpl.language,
        "language_label": R.language_label(tpl.language),
        "category": tpl.category,
        "template_type": template_type,
        "template_type_label": R.template_type_label(tpl.category, template_type),
        "purpose": tpl.purpose,
        "variable_type": _variable_type(tpl),
        "header_format": tpl.header_format,
        "header_type": tpl.header_format or "NONE",
        "header_text": tpl.header_text,
        "header_sample_values": tpl.header_sample_values or {},
        "header_media": _media_out(tpl.header_media),
        "body_text": tpl.body_text_named,
        "body_text_positional": tpl.body_text_positional,
        "footer_text": tpl.footer_text,
        "buttons": tpl.buttons or [],
        "button_types": [(b.get("type") or "").upper() for b in (tpl.buttons or [])]
                        or (["OTP"] if tpl.auth_config else []),
        "button_summary": button_summary(tpl.buttons, tpl.auth_config),
        "variables": keys,
        "sample_values": tpl.sample_values or {},
        "limited_time_offer": tpl.limited_time_offer,
        "carousel_cards": [{**c, "media": _media_out(c.get("media"))}
                           for c in (tpl.carousel_cards or [])] or None,
        "auth_config": tpl.auth_config,
        "custom_validity_period": bool(tpl.custom_validity_period),
        "validity_period_seconds": tpl.validity_period_seconds,
        "time_to_live": time_to_live(tpl),
        "provider_template_id": tpl.provider_template_id,
        "approval_status": tpl.approval_status,
        "status": (tpl.approval_status or "").upper(),
        "rejection_reason": tpl.rejection_reason,
        "previous_category": tpl.previous_category,
        "last_synced_at": _iso(tpl.last_synced_at),
        "submitted_at": _iso(tpl.submitted_at),
        "is_active": bool(tpl.is_active),
        "version": tpl.version,
        "in_use_by_rules": in_use,
        "warnings": template_warnings(data),
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


def _positional(text: Optional[str], variable_type: str) -> Tuple[str, List[str]]:
    if variable_type == "NUMBER":
        return text or "", R.numbered_keys(text)
    return to_positional(text)


def _spec_button(b: Dict[str, Any]) -> Dict[str, Any]:
    """Editor button → provider button: a URL variable becomes {{1}} (Meta)."""
    out = {k: v for k, v in b.items() if v not in (None, "")}
    if out.get("type") == "URL" and PLACEHOLDER_RE.search(out.get("url") or ""):
        out["url"] = PLACEHOLDER_RE.sub("{{1}}", out["url"])
        out["example"] = clean_param(out.pop("url_example", None))
    if out.get("type") == "PHONE_NUMBER" and out.get("phone_number"):
        out["phone_number"] = re.sub(r"[\s()-]", "", out["phone_number"])
    return out


def build_spec(tpl, header_file: Optional[Tuple[str, bytes, str]] = None):
    from app.whatsapp_layer.providers.base import TemplateSpec
    vt = _variable_type(tpl)
    vm = tpl.variable_map or build_variable_map(tpl.header_format, tpl.header_text,
                                                tpl.body_text_named, vt)
    samples = tpl.sample_values or {}
    header_samples = tpl.header_sample_values or {}
    header_pos = _positional(tpl.header_text, vt)[0] if tpl.header_format == "TEXT" else None
    cards = []
    for c in ((tpl.carousel_cards or []) if tpl.header_format == "CAROUSEL" else []):
        body, keys = _positional(c.get("body_text"), vt)
        cs = c.get("sample_values") or {}
        media = c.get("media") or {}
        cards.append({"media_type": (media.get("media_type") or "IMAGE").upper(),
                      "media_url": media_url(media), "body_text": body,
                      "body_example": [clean_param(cs.get(k)) for k in keys],
                      "buttons": [_spec_button(b) for b in c.get("buttons") or []]})
    ttl = None
    custom = False
    cfg = tpl.auth_config or None
    if tpl.category == "AUTHENTICATION" and cfg and cfg.get("code_expiration_minutes"):
        custom, ttl = True, int(cfg["code_expiration_minutes"])
    elif tpl.custom_validity_period and tpl.validity_period_seconds:
        # UNCONFIRMED unit: the vendor doc says timeToLive is in minutes.
        custom, ttl = True, max(1, -(-int(tpl.validity_period_seconds) // 60))
    return TemplateSpec(
        name=tpl.name,
        language=tpl.language,
        category=tpl.category,
        template_type=_template_type(tpl),
        header_format=tpl.header_format or "NONE",
        header_text=header_pos,
        header_example=[clean_param(header_samples.get(k) or samples.get(k))
                        for k in (vm.get("header") or [])],
        header_file=header_file,
        body_text=tpl.body_text_positional or _positional(tpl.body_text_named, vt)[0],
        body_example=[clean_param(samples.get(k)) for k in (vm.get("body") or [])],
        footer_text=tpl.footer_text,
        buttons=[_spec_button(b) for b in (tpl.buttons or [])],
        limited_time_offer=tpl.limited_time_offer or None,
        carousel_cards=cards,
        auth_config=cfg,
        custom_validity_period=custom,
        time_to_live=ttl,
    )


def submit(db: Session, tpl, account, user_id: Optional[int]) -> None:
    """Send a draft/rejected template to the provider; becomes `pending`.
    Raises ProviderError on failure (the API maps it to 502/409) and
    MediaError when the header file cannot be read (422)."""
    from app.whatsapp_layer import accounts as acct_svc
    from app.whatsapp_layer.providers.registry import get_provider
    apply_fields(tpl, {})  # make sure positional + variable_map are current
    header_file = None
    if tpl.header_format in MEDIA_HEADERS and tpl.header_media:
        header_file = fetch_media(tpl.header_media, tpl.header_format)
    provider = get_provider(account.provider)
    result = provider.create_template(acct_svc.credentials(account), build_spec(tpl, header_file))
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
    if ptpl.parameter_format and ptpl.parameter_format != tpl.parameter_format:
        tpl.parameter_format = ptpl.parameter_format
        changed = True
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
    if "untracked_list" in keys:      # the end of day report is looked up by name
        return "generic"
    if "candidate_list" in keys:
        return "team_digest" if "recipient_name" in keys else "recruiter_batch"
    if "missed_count" in keys or "recipient_name" in keys:
        return "team_digest"
    if "due_count" in keys:
        return "recruiter_batch"
    if "candidate_name" in keys:
        return "final_validation" if "final_validation" in name else "recruiter_reminder"
    return "generic"


def copy_provider_content(tpl, ptpl) -> None:
    """Make a template match what the provider holds, so the variables we
    send line up with the approved body."""
    # A purpose already chosen stays; only unassigned templates are worked out.
    if not tpl.purpose or tpl.purpose == "generic":
        tpl.purpose = purpose_for_import(tpl.name, clean_provider_body(ptpl.body_text))
    header_format = (ptpl.header_format or "").upper()
    if header_format not in HEADER_FORMATS or header_format == "CAROUSEL":
        header_format = "TEXT" if ptpl.header_text else "NONE"
    fields = {
        "header_format": header_format,
        "header_text": ptpl.header_text,
        "body_text": clean_provider_body(ptpl.body_text),
        "footer_text": ptpl.footer_text,
        "sample_values": ptpl.sample_values or None,
        "variable_type": None,      # worked out from the provider's body
    }
    if ptpl.template_type:
        fields["template_type"] = ptpl.template_type
    if ptpl.buttons is not None:
        fields["buttons"] = ptpl.buttons
    if header_format in MEDIA_HEADERS and ptpl.header_media:
        fields["header_media"] = ptpl.header_media
    apply_fields(tpl, fields)


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
        # Templates are per account (WABA): another account's template of the
        # same name is a different template. Drafts without an account match.
        tpl = (db.query(T).filter(T.organization_id == account.organization_id, T.name == ptpl.name,
                                  T.language == language, T.deleted_at.is_(None),
                                  or_(T.account_id == account.id, T.account_id.is_(None)))
               .order_by(T.account_id.is_(None), T.id.asc()).first())
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
        # Numbered bodies on the provider stay as they are: HRMIS keeps the named form.
        edited = ((ptpl.parameter_format or "").upper() == "NAMED"
                  and clean_provider_body(ptpl.body_text) != tpl.body_text_named)
        if tpl.approval_status == "draft" or edited:
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
    accounts = db.query(A).filter(A.status == "active", A.deleted_at.is_(None))
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
        account = db.query(A).filter(A.id == tpl.account_id, A.deleted_at.is_(None)).first()
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


# ---------------------------------------------------------------------------
# Media upload (POST /templates/media)
# ---------------------------------------------------------------------------
_EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".mp4": "video/mp4", ".3gp": "video/3gpp", ".pdf": "application/pdf"}


def media_kind(mime: str, file_name: str) -> Tuple[Optional[str], str]:
    """(IMAGE|VIDEO|DOCUMENT or None, resolved mime)."""
    import os
    mime = (mime or "").lower().split(";")[0]
    if not any(mime in allowed for allowed in R.MEDIA_MIME.values()):
        mime = _EXT_MIME.get(os.path.splitext(file_name or "")[1].lower(), mime)
    for kind, allowed in R.MEDIA_MIME.items():
        if mime in allowed:
            return kind, mime
    return None, mime


def upload_media(organization_id: int, data: bytes, mime: str, file_name: str,
                 media_type: Optional[str] = None) -> Dict[str, Any]:
    """Store a header / carousel file in S3. Raises MediaError (422) for a bad
    file and RuntimeError when S3 is not configured."""
    import uuid
    from app.chat_layer import s3_chat_service as s3
    from app.core import settings
    kind, mime = media_kind(mime, file_name)
    if kind is None:
        raise MediaError("Use JPG/PNG images, MP4/3GP videos or PDF documents")
    if media_type and media_type.upper() != kind:
        raise MediaError(f"This file is a {kind.lower()}, not a {media_type.lower()}")
    if len(data) > R.MEDIA_MAX_BYTES[kind]:
        raise MediaError(f"{kind.title()} files must be at most "
                         f"{R.MEDIA_MAX_BYTES[kind] // (1024 * 1024)} MB")
    if not s3._is_configured():
        raise RuntimeError("File storage is not configured")
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in (file_name or "") else "bin"
    key = f"whatsapp_templates/{organization_id}/{datetime.utcnow():%Y-%m}/{uuid.uuid4().hex}.{ext}"
    s3._client().put_object(Bucket=settings.AWS_S3_BUCKET_CHAT, Key=key, Body=data,
                            ContentType=mime)
    media = {"s3_key": key, "file_name": file_name, "mime_type": mime,
             "size_bytes": len(data), "media_type": kind}
    return _media_out(media)


def template_options() -> Dict[str, Any]:
    """Enums and limits for the editor (GET /templates/options)."""
    return {
        "categories": list(R.CATEGORIES),
        "template_types": {k: list(v) for k, v in R.TEMPLATE_TYPES.items()},
        "variable_types": list(R.VARIABLE_TYPES),
        "header_formats": list(R.HEADER_FORMATS),
        "button_types": [{"type": t, "label": R.BUTTON_LABELS[t], "max": R.BUTTON_CAPS[t],
                          "max_in_utility": R.UTILITY_URL_CAP if t == "URL" else R.BUTTON_CAPS[t],
                          "available_in": [{"category": c, "template_type": tt}
                                           for c, tt in R.BUTTON_AVAILABILITY[t]]}
                         for t in R.BUTTON_TYPES],
        "code_delivery": list(R.CODE_DELIVERY),
        "flow_actions": list(R.FLOW_ACTIONS),
        "carousel_button_types": list(R.CARD_BUTTON_TYPES),
        "media": {k: {"mime_types": list(v), "max_bytes": R.MEDIA_MAX_BYTES[k]}
                  for k, v in R.MEDIA_MIME.items()},
        "languages": [{"code": k, "label": v} for k, v in R.LANGUAGE_LABELS.items()],
        "statuses": [s.upper() for s in APPROVAL_STATUSES],
        "limits": {
            "name": 512, "header": R.HEADER_MAX, "body": R.BODY_MAX, "footer": R.FOOTER_MAX,
            "button_label": R.BUTTON_LABEL_MAX, "button_url": R.BUTTON_URL_MAX,
            "buttons_total": R.MAX_BUTTONS, "copy_code": R.COPY_CODE_MAX,
            "lto_text": R.LTO_TEXT_MAX, "cards_min": R.CARD_MIN, "cards_max": R.CARD_MAX,
            "card_body": R.CARD_BODY_MAX, "card_media_url": R.CARD_MEDIA_URL_MAX,
            "card_button_label": R.CARD_BUTTON_LABEL_MAX, "card_button_url": R.CARD_BUTTON_URL_MAX,
            "card_buttons": R.CARD_BUTTONS_MAX, "package_name": R.PACKAGE_NAME_MAX,
            "signature_hash": R.SIGNATURE_HASH_LEN, "auth_apps": R.AUTH_APPS_MAX,
            "code_expiration_minutes": [R.CODE_EXPIRY_MIN, R.CODE_EXPIRY_MAX],
            "validity_period_seconds": [R.VALIDITY_MIN, R.VALIDITY_MAX],
        },
        "name_pattern": R.NAME_RE.pattern,
        "name_error": R.NAME_ERROR,
        "placeholder_url_warning": R.PLACEHOLDER_URL_WARNING,
    }
