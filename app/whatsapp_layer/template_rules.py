"""WhatsApp template editor rules (BroadNet "Templates" product, docs/TEMPLATES_CONTRACT.md).

Pure functions only: constants, normalisation of the editor payload,
validation (errors that block) and warnings (shown, never blocking).
`templates_service` re-exports what callers need.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.whatsapp_layer import variables as V

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_ADJACENT_RE = re.compile(r"\}\}\s*\{\{")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,511}$")
NAME_ERROR = ("Template name must start with a lowercase letter, contain only [a-z0-9_] "
              "and be max 512 chars.")
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(_[A-Za-z]{2,4})?$")
NAMED_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
SIGNATURE_RE = re.compile(r"^[A-Za-z0-9+/=_-]{11}$")
PHONE_RE = re.compile(r"^\+?[1-9]\d{6,19}$")

# --- Limits ------------------------------------------------------------------
BODY_MAX = 1024
HEADER_MAX = 60
FOOTER_MAX = 60
BUTTON_LABEL_MAX = 25
BUTTON_URL_MAX = 2000
MAX_BUTTONS = 10              # Meta's overall cap; per-type caps below
COPY_CODE_MAX = 15            # Meta: offer code example max 15 chars
LTO_TEXT_MAX = 16             # Meta: limited_time_offer.text max 16 chars
CARD_MIN = 1
CARD_MAX = 10
CARD_BODY_MAX = 1024
CARD_MEDIA_URL_MAX = 1600
CARD_BUTTON_LABEL_MAX = 25
CARD_BUTTON_URL_MAX = 2000
CARD_BUTTONS_MAX = 2
PACKAGE_NAME_MAX = 224
SIGNATURE_HASH_LEN = 11
AUTH_APPS_MAX = 5
CODE_EXPIRY_MIN, CODE_EXPIRY_MAX = 1, 90            # minutes
VALIDITY_MIN, VALIDITY_MAX = 30, 43200              # seconds (Meta utility TTL)
FLOW_SCREEN_MAX = 64

# --- Enums -------------------------------------------------------------------
CATEGORIES = ("MARKETING", "UTILITY", "AUTHENTICATION")
TEMPLATE_TYPES: Dict[str, Tuple[str, ...]] = {
    "MARKETING": ("DEFAULT", "CATALOGUE", "CALLING_PERMISSION_REQUEST"),
    "UTILITY": ("DEFAULT", "CALLING_PERMISSION_REQUEST"),
    "AUTHENTICATION": ("OTP",),
}
VARIABLE_TYPES = ("NUMBER", "NAME")
HEADER_FORMATS = ("NONE", "TEXT", "IMAGE", "VIDEO", "DOCUMENT", "LOCATION", "CAROUSEL")
MEDIA_HEADERS = ("IMAGE", "VIDEO", "DOCUMENT")
BUTTON_TYPES = ("PHONE_NUMBER", "URL", "VOICE_CALL", "COPY_CODE", "MPM", "FLOW", "QUICK_REPLY")
BUTTON_CAPS = {"PHONE_NUMBER": 1, "URL": 2, "VOICE_CALL": 1, "COPY_CODE": 1, "MPM": 1,
               "FLOW": 1, "QUICK_REPLY": 5}
UTILITY_URL_CAP = 1
# (category, template_type or None = any sub-type)
BUTTON_AVAILABILITY: Dict[str, Tuple[Tuple[str, Optional[str]], ...]] = {
    "PHONE_NUMBER": (("MARKETING", None), ("UTILITY", None)),
    "URL": (("MARKETING", None), ("UTILITY", None)),
    "VOICE_CALL": (("MARKETING", None), ("UTILITY", None)),
    "COPY_CODE": (("MARKETING", None),),
    "MPM": (("MARKETING", "DEFAULT"),),
    "FLOW": (("MARKETING", "DEFAULT"),),
    "QUICK_REPLY": (("MARKETING", None), ("UTILITY", None)),
}
BUTTON_LABELS = {"PHONE_NUMBER": "Phone number", "URL": "Url", "VOICE_CALL": "Voice Call",
                 "COPY_CODE": "Copy Code", "MPM": "Multi-product", "FLOW": "Flow",
                 "QUICK_REPLY": "Quick Reply", "OTP": "otp"}
FLOW_ACTIONS = ("navigate", "data_exchange")
CARD_MEDIA_TYPES = ("IMAGE", "VIDEO")
CARD_BUTTON_TYPES = ("URL", "QUICK_REPLY", "PHONE_NUMBER")
CODE_DELIVERY = ("ZERO_TAP", "ONE_TAP", "COPY_CODE")
MEDIA_MIME = {
    "IMAGE": ("image/jpeg", "image/png"),
    "VIDEO": ("video/mp4", "video/3gpp"),
    "DOCUMENT": ("application/pdf",),
}
MEDIA_MAX_BYTES = {"IMAGE": 5 * 1024 * 1024, "VIDEO": 16 * 1024 * 1024,
                   "DOCUMENT": 100 * 1024 * 1024}
PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net")
PLACEHOLDER_URL_WARNING = ("Warning: Meta may reject templates with placeholder URLs "
                           "(e.g., example.com). Please use a real, valid URL.")
AUTH_SECURITY_TEXT = "For your security, do not share this code."
AUTH_BODY = "{{1}} is your verification code."
AUTH_DEFAULT_BUTTON = "Copy Code"

LANGUAGE_LABELS = {
    "en": "English", "en_US": "English (US)", "en_GB": "English (UK)", "hi": "Hindi",
    "ar": "Arabic", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
    "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "pa": "Punjabi", "ur": "Urdu",
    "es": "Spanish", "fr": "French", "de": "German", "pt_BR": "Portuguese (BR)",
    "id": "Indonesian", "ms": "Malay",
}


def language_label(code: Optional[str]) -> Optional[str]:
    return LANGUAGE_LABELS.get(code or "", code)


def template_type_label(category: Optional[str], template_type: Optional[str]) -> str:
    """List column "Template type": DEFAULT, UTILITY DEFAULT, OTP, ..."""
    if not template_type:
        return "-"
    if category == "UTILITY" and template_type == "DEFAULT":
        return "UTILITY DEFAULT"
    if category == "UTILITY":
        return f"UTILITY {template_type}"
    return template_type


# ---------------------------------------------------------------------------
# Placeholders
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


def infer_variable_type(*texts: Optional[str]) -> str:
    keys = [k for t in texts for k in extract_keys(t)]
    if keys and all(k.isdigit() for k in keys):
        return "NUMBER"
    return "NAME" if keys else "NUMBER"


def numbered_keys(text: Optional[str]) -> List[str]:
    """Numeric keys sorted by number ({{2}} {{1}} → ["1", "2"])."""
    return sorted({k for k in extract_keys(text) if k.isdigit()}, key=int)


def _err(field: str, message: str) -> Dict[str, str]:
    return {"field": field, "message": message}


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _clean_dict(d: Any) -> Dict[str, Any]:
    return {k: v for k, v in (d or {}).items() if v is not None} if isinstance(d, dict) else {}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def normalise_button(b: Any) -> Dict[str, Any]:
    b = _clean_dict(b)
    if not b.get("label") and b.get("text"):
        b["label"] = b.pop("text")
    b.pop("text", None)
    b["type"] = _s(b.get("type")).strip().upper()
    if b["type"] == "COPY_CODE" and not _s(b.get("label")).strip():
        b["label"] = "Copy offer code"
    if b["type"] == "FLOW" and b.get("flow_action"):
        b["flow_action"] = _s(b["flow_action"]).strip().lower()
    return b


def normalise(data: Dict[str, Any]) -> Dict[str, Any]:
    """Uppercase enums, fill sub-type defaults, drop sections the category or
    header type does not use. Returns a new dict."""
    d = dict(data)
    d["category"] = _s(d.get("category") or "UTILITY").strip().upper()
    d["header_format"] = _s(d.get("header_format") or "NONE").strip().upper()
    tt = _s(d.get("template_type")).strip().upper() or None
    if tt == "CALLING_PERMISSION":
        tt = "CALLING_PERMISSION_REQUEST"
    if tt is None:
        tt = "OTP" if d["category"] == "AUTHENTICATION" else "DEFAULT"
    d["template_type"] = tt
    d["buttons"] = [normalise_button(b) for b in (d.get("buttons") or [])] \
        if isinstance(d.get("buttons"), list) else d.get("buttons")
    d["sample_values"] = _clean_dict(d.get("sample_values"))
    d["header_sample_values"] = _clean_dict(d.get("header_sample_values"))
    d["custom_validity_period"] = _bool(d.get("custom_validity_period"))
    if not d["custom_validity_period"]:
        d["validity_period_seconds"] = None

    if d["category"] == "AUTHENTICATION":
        # Meta fixes the body; no header, footer, buttons, LTO or carousel.
        cfg = _clean_dict(d.get("auth_config"))
        if cfg.get("code_delivery"):
            cfg["code_delivery"] = _s(cfg["code_delivery"]).strip().upper()
        cfg["apps"] = [_clean_dict(a) for a in (cfg.get("apps") or []) if isinstance(a, dict)]
        cfg["add_security_recommendation"] = _bool(cfg.get("add_security_recommendation"))
        d["auth_config"] = cfg
        d["variable_type"] = "NUMBER"
        d["header_format"] = "NONE"
        d["header_text"] = None
        d["header_media"] = None
        d["body_text"] = AUTH_BODY + (" " + AUTH_SECURITY_TEXT if cfg["add_security_recommendation"] else "")
        d["footer_text"] = None
        d["buttons"] = []
        d["limited_time_offer"] = None
        d["carousel_cards"] = None
    else:
        d["auth_config"] = None
        vt = _s(d.get("variable_type")).strip().upper() or None
        d["variable_type"] = vt or infer_variable_type(d.get("header_text"), d.get("body_text"))
        lto = d.get("limited_time_offer")
        if isinstance(lto, dict):
            lto = _clean_dict(lto)
            lto["has_expiration"] = _bool(lto.get("has_expiration"))
            if lto.get("enabled") is not None and not _bool(lto.pop("enabled")):
                lto = None
            else:
                lto.pop("enabled", None)
        d["limited_time_offer"] = lto or None
    if d["header_format"] != "TEXT":
        d["header_text"] = None
        d["header_sample_values"] = {}
    if d["header_format"] not in MEDIA_HEADERS:
        d["header_media"] = None
    elif isinstance(d.get("header_media"), dict):
        d["header_media"] = _clean_dict(d["header_media"]) or None
    if d["header_format"] == "CAROUSEL":
        cards = d.get("carousel_cards")
        if isinstance(cards, list):
            out = []
            for c in cards:
                c = _clean_dict(c)
                if isinstance(c.get("media"), dict):
                    c["media"] = _clean_dict(c["media"])
                    if c["media"].get("media_type"):
                        c["media"]["media_type"] = _s(c["media"]["media_type"]).upper()
                c["sample_values"] = _clean_dict(c.get("sample_values"))
                c["buttons"] = [normalise_button(b) for b in (c.get("buttons") or [])]
                out.append(c)
            d["carousel_cards"] = out
    else:
        d["carousel_cards"] = None
    return d


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _valid_http_url(url: str) -> bool:
    p = urlparse(url or "")
    return p.scheme in ("http", "https") and bool(p.netloc)


def _parse_date(value: Any) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    s = _s(value).strip()
    if not s:
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _check_style(field: str, keys: List[str], variable_type: str,
                 errors: List[Dict[str, str]]) -> None:
    for k in keys:
        if variable_type == "NUMBER" and not k.isdigit():
            errors.append(_err(field, "Only numeric variables such as {{1}} are allowed when the "
                                      f"variable type is Number (found '{{{{{k}}}}}')"))
        elif variable_type == "NAME" and (k.isdigit() or not NAMED_KEY_RE.match(k)):
            errors.append(_err(field, "Only named variables such as {{name}} are allowed when the "
                                      f"variable type is Name (found '{{{{{k}}}}}')"))


def _check_sequence(field: str, keys: List[str], errors: List[Dict[str, str]]) -> None:
    nums = sorted({int(k) for k in keys if k.isdigit()})
    if nums and nums != list(range(1, len(nums) + 1)):
        errors.append(_err(field, "Numbered variables must run {{1}}, {{2}}, {{3}}... without gaps"))


def _check_body_shape(field: str, body: str, errors: List[Dict[str, str]]) -> None:
    stripped = body.strip()
    if stripped:
        if PLACEHOLDER_RE.match(stripped):
            errors.append(_err(field, "Body must not start with a variable"))
        if re.search(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}$", stripped):
            errors.append(_err(field, "Body must not end with a variable"))
    if _ADJACENT_RE.search(body):
        errors.append(_err(field, "Two variables must not be next to each other"))


def body_length(body: str, variable_type: str) -> int:
    """Length as Meta counts it (named variables become {{n}})."""
    return len(to_positional(body)[0]) if variable_type == "NAME" else len(body)


def _validate_media(field: str, media: Any, kind: str, errors: List[Dict[str, str]],
                    *, for_submit: bool, url_max: int = BUTTON_URL_MAX) -> None:
    media = media if isinstance(media, dict) else {}
    url, key = _s(media.get("url")).strip(), _s(media.get("s3_key")).strip()
    if not url and not key:
        if for_submit:
            errors.append(_err(field, "Upload a file or give a media URL"))
        return
    if url and not key:
        if len(url) > url_max:
            errors.append(_err(f"{field}.url", f"Media URL must be at most {url_max} characters"))
        elif not _valid_http_url(url):
            errors.append(_err(f"{field}.url", "Media URL must be an http(s) URL"))
    mime = _s(media.get("mime_type")).lower()
    if mime and kind in MEDIA_MIME and mime not in MEDIA_MIME[kind]:
        errors.append(_err(f"{field}.mime_type",
                           f"{kind.title()} must be one of {', '.join(MEDIA_MIME[kind])}"))


def _validate_button(field: str, b: Dict[str, Any], variable_type: str,
                     errors: List[Dict[str, str]], *, for_submit: bool) -> None:
    btype = b.get("type") or ""
    label = _s(b.get("label")).strip()
    if not label:
        errors.append(_err(f"{field}.label", "Button text is required"))
    elif len(label) > BUTTON_LABEL_MAX:
        errors.append(_err(f"{field}.label", f"Button text must be at most {BUTTON_LABEL_MAX} characters"))
    if PLACEHOLDER_RE.search(label):
        errors.append(_err(f"{field}.label", "Button text must not contain variables"))
    if btype == "URL":
        url = _s(b.get("url")).strip()
        if not url:
            errors.append(_err(f"{field}.url", "A URL button needs a website URL"))
            return
        if len(url) > BUTTON_URL_MAX:
            errors.append(_err(f"{field}.url", f"URL must be at most {BUTTON_URL_MAX} characters"))
        if not re.match(r"^https?://[^/\s{}]+", url):
            errors.append(_err(f"{field}.url", "A URL button needs a valid http(s) website URL"))
        keys = PLACEHOLDER_RE.findall(url)
        if len(keys) > 1:
            errors.append(_err(f"{field}.url", "A URL may contain only one variable"))
        elif keys:
            if not re.search(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}$", url):
                errors.append(_err(f"{field}.url", "The URL variable must be at the end of the URL"))
            _check_style(f"{field}.url", keys, variable_type, errors)
            if for_submit and not _s(b.get("url_example")).strip():
                errors.append(_err(f"{field}.url_example", "Sample value required for the URL variable"))
    elif btype == "PHONE_NUMBER":
        phone = re.sub(r"[\s()-]", "", _s(b.get("phone_number")))
        if not PHONE_RE.match(phone):
            errors.append(_err(f"{field}.phone_number",
                               "Give the phone number with its country code, e.g. +911234567890"))
    elif btype == "COPY_CODE":
        code = _s(b.get("example")).strip()
        if not code:
            errors.append(_err(f"{field}.example", "Offer code is required"))
        elif len(code) > COPY_CODE_MAX:
            errors.append(_err(f"{field}.example", f"Offer code must be at most {COPY_CODE_MAX} characters"))
    elif btype == "FLOW":
        if not _s(b.get("flow_id")).strip():
            errors.append(_err(f"{field}.flow_id", "Choose a WhatsApp Flow"))
        action = _s(b.get("flow_action") or "navigate")
        if action not in FLOW_ACTIONS:
            errors.append(_err(f"{field}.flow_action", f"Must be one of {', '.join(FLOW_ACTIONS)}"))
        screen = _s(b.get("navigate_screen")).strip()
        if action == "navigate" and not screen:
            errors.append(_err(f"{field}.navigate_screen", "Screen ID is required for the navigate action"))
        elif len(screen) > FLOW_SCREEN_MAX:
            errors.append(_err(f"{field}.navigate_screen", f"Screen ID must be at most {FLOW_SCREEN_MAX} characters"))


def _validate_buttons(buttons: Any, category: str, template_type: str, variable_type: str,
                      errors: List[Dict[str, str]], *, for_submit: bool) -> None:
    if not isinstance(buttons, list):
        errors.append(_err("buttons", "Buttons must be a list"))
        return
    if len(buttons) > MAX_BUTTONS:
        errors.append(_err("buttons", f"At most {MAX_BUTTONS} buttons"))
    counts: Dict[str, int] = {}
    for i, b in enumerate(buttons):
        b = b if isinstance(b, dict) else {}
        btype = b.get("type") or ""
        field = f"buttons[{i}]"
        if btype not in BUTTON_TYPES:
            errors.append(_err(f"{field}.type", f"Must be one of {', '.join(BUTTON_TYPES)}"))
            continue
        counts[btype] = counts.get(btype, 0) + 1
        allowed = BUTTON_AVAILABILITY[btype]
        if not any(c == category and (t is None or t == template_type) for c, t in allowed):
            where = " or ".join(f"{c.title()}{'' if t is None else ' ' + t.title()}" for c, t in allowed)
            errors.append(_err(f"{field}.type",
                               f"{BUTTON_LABELS[btype]} buttons are only available in {where} templates"))
        _validate_button(field, b, variable_type, errors, for_submit=for_submit)
    for btype, n in counts.items():
        cap = UTILITY_URL_CAP if (btype == "URL" and category == "UTILITY") else BUTTON_CAPS[btype]
        if n > cap:
            suffix = " in Utility templates" if (btype == "URL" and category == "UTILITY") else ""
            errors.append(_err("buttons", f"At most {cap} {BUTTON_LABELS[btype]} "
                                          f"button{'s' if cap > 1 else ''}{suffix}"))


def _validate_carousel(cards: Any, variable_type: str, errors: List[Dict[str, str]],
                       *, for_submit: bool) -> None:
    if not isinstance(cards, list) or len(cards) < CARD_MIN:
        errors.append(_err("carousel_cards", "Add at least one carousel card"))
        return
    if len(cards) > CARD_MAX:
        errors.append(_err("carousel_cards", f"At most {CARD_MAX} carousel cards"))
    for i, c in enumerate(cards):
        field = f"carousel_cards[{i}]"
        c = c if isinstance(c, dict) else {}
        media = c.get("media") if isinstance(c.get("media"), dict) else {}
        mtype = _s(media.get("media_type") or "IMAGE").upper()
        if mtype not in CARD_MEDIA_TYPES:
            errors.append(_err(f"{field}.media.media_type", "Card media must be IMAGE or VIDEO"))
        _validate_media(f"{field}.media", media, mtype, errors, for_submit=for_submit,
                        url_max=CARD_MEDIA_URL_MAX)
        body = _s(c.get("body_text"))
        if len(body) > CARD_BODY_MAX:
            errors.append(_err(f"{field}.body_text", f"Card body must be at most {CARD_BODY_MAX} characters"))
        keys = extract_keys(body)
        _check_style(f"{field}.body_text", keys, variable_type, errors)
        if variable_type == "NUMBER":
            _check_sequence(f"{field}.body_text", keys, errors)
        if for_submit:
            samples = c.get("sample_values") or {}
            for k in keys:
                if not _s(samples.get(k)).strip():
                    errors.append(_err(f"{field}.sample_values", f"Sample value required for '{{{{{k}}}}}'"))
        buttons = c.get("buttons") or []
        if not isinstance(buttons, list):
            errors.append(_err(f"{field}.buttons", "Buttons must be a list"))
            continue
        if len(buttons) > CARD_BUTTONS_MAX:
            errors.append(_err(f"{field}.buttons", f"At most {CARD_BUTTONS_MAX} buttons per card"))
        for j, b in enumerate(buttons):
            b = b if isinstance(b, dict) else {}
            bf = f"{field}.buttons[{j}]"
            btype = b.get("type") or ""
            if btype not in CARD_BUTTON_TYPES:
                errors.append(_err(f"{bf}.type", f"Must be one of {', '.join(CARD_BUTTON_TYPES)}"))
                continue
            label = _s(b.get("label")).strip()
            if not label:
                errors.append(_err(f"{bf}.label", "Button text is required"))
            elif len(label) > CARD_BUTTON_LABEL_MAX:
                errors.append(_err(f"{bf}.label", f"Button text must be at most {CARD_BUTTON_LABEL_MAX} characters"))
            if btype == "URL":
                url = _s(b.get("url")).strip()
                if not url or not _valid_http_url(url.split("{{")[0] + "x"):
                    errors.append(_err(f"{bf}.url", "A URL button needs a valid http(s) website URL"))
                elif len(url) > CARD_BUTTON_URL_MAX:
                    errors.append(_err(f"{bf}.url", f"URL must be at most {CARD_BUTTON_URL_MAX} characters"))
            elif btype == "PHONE_NUMBER":
                if not PHONE_RE.match(re.sub(r"[\s()-]", "", _s(b.get("phone_number")))):
                    errors.append(_err(f"{bf}.phone_number",
                                       "Give the phone number with its country code, e.g. +911234567890"))


def _validate_auth(cfg: Any, errors: List[Dict[str, str]]) -> None:
    cfg = cfg if isinstance(cfg, dict) else {}
    delivery = cfg.get("code_delivery")
    if delivery not in CODE_DELIVERY:
        errors.append(_err("auth_config.code_delivery", f"Must be one of {', '.join(CODE_DELIVERY)}"))
    apps = cfg.get("apps") or []
    if delivery in ("ZERO_TAP", "ONE_TAP") and not apps:
        errors.append(_err("auth_config.apps", "Add at least one Android app for zero-tap or one-tap autofill"))
    if len(apps) > AUTH_APPS_MAX:
        errors.append(_err("auth_config.apps", f"At most {AUTH_APPS_MAX} apps"))
    for i, a in enumerate(apps):
        pkg = _s(a.get("package_name")).strip()
        sig = _s(a.get("signature_hash")).strip()
        if not pkg:
            errors.append(_err(f"auth_config.apps[{i}].package_name", "Package name is required"))
        elif len(pkg) > PACKAGE_NAME_MAX:
            errors.append(_err(f"auth_config.apps[{i}].package_name",
                               f"Package name must be at most {PACKAGE_NAME_MAX} characters"))
        elif not PACKAGE_RE.match(pkg):
            errors.append(_err(f"auth_config.apps[{i}].package_name",
                               "Use an Android package name such as com.example.app"))
        if len(sig) != SIGNATURE_HASH_LEN or not SIGNATURE_RE.match(sig):
            errors.append(_err(f"auth_config.apps[{i}].signature_hash",
                               f"App signature hash must be exactly {SIGNATURE_HASH_LEN} characters"))
    exp = cfg.get("code_expiration_minutes")
    if exp not in (None, ""):
        try:
            ok = CODE_EXPIRY_MIN <= int(exp) <= CODE_EXPIRY_MAX
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errors.append(_err("auth_config.code_expiration_minutes",
                               f"Expiry must be {CODE_EXPIRY_MIN}–{CODE_EXPIRY_MAX} minutes"))
    for key in ("button_text", "autofill_text"):
        if len(_s(cfg.get(key))) > BUTTON_LABEL_MAX:
            errors.append(_err(f"auth_config.{key}", f"Must be at most {BUTTON_LABEL_MAX} characters"))


def validate_template(data: Dict[str, Any], *, for_submit: bool = False) -> List[Dict[str, str]]:
    """Returns a list of {field, message}; empty = valid. Sample values and
    media are only required when `for_submit`."""
    d = normalise(data)
    errors: List[Dict[str, str]] = []
    name = _s(d.get("name"))
    purpose = _s(d.get("purpose"))
    category = d["category"]
    template_type = d["template_type"]
    variable_type = d["variable_type"]
    header_format = d["header_format"]
    header_text = _s(d.get("header_text"))
    body = _s(d.get("body_text"))
    footer = _s(d.get("footer_text"))
    samples = d.get("sample_values") or {}
    header_samples = d.get("header_sample_values") or {}

    if not NAME_RE.match(name):
        errors.append(_err("name", NAME_ERROR))
    if not LANGUAGE_RE.match(_s(d.get("language"))):
        errors.append(_err("language", "Use a WhatsApp language code such as en_US"))
    if category not in CATEGORIES:
        errors.append(_err("category", f"Must be one of {', '.join(CATEGORIES)}"))
    elif template_type not in TEMPLATE_TYPES[category]:
        errors.append(_err("template_type", f"For {category.title()} use one of "
                                            f"{', '.join(TEMPLATE_TYPES[category])}"))
    if purpose not in V.PURPOSES:
        errors.append(_err("purpose", f"Must be one of {', '.join(V.PURPOSES)}"))
    if variable_type not in VARIABLE_TYPES:
        errors.append(_err("variable_type", "Must be NUMBER or NAME"))
        variable_type = "NAME"
    elif purpose in V.PURPOSES and purpose != "generic" and variable_type != "NAME":
        errors.append(_err("variable_type", "Clawback templates use named variables (NAME)"))
    if header_format not in HEADER_FORMATS:
        errors.append(_err("header_format", f"Must be one of {', '.join(HEADER_FORMATS)}"))

    # Validity period (Utility only).
    if d.get("custom_validity_period"):
        if category != "UTILITY":
            errors.append(_err("custom_validity_period",
                               "A custom validity period is only available for Utility templates"))
        try:
            secs = int(d.get("validity_period_seconds"))
            ok = VALIDITY_MIN <= secs <= VALIDITY_MAX
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errors.append(_err("validity_period_seconds",
                               f"Validity must be {VALIDITY_MIN}–{VALIDITY_MAX} seconds"))

    if category == "AUTHENTICATION":
        _validate_auth(d.get("auth_config"), errors)
        return errors

    # Catalogue keys only bind clawback purposes; generic templates may use any name.
    allowed = set(V.allowed_keys(purpose)) if purpose in V.PURPOSES else set()
    strict = purpose in V.PURPOSES and purpose != "generic"

    def _check_keys(field: str, keys: List[str]) -> None:
        if not strict:
            return
        for k in keys:
            if k not in V.VARIABLES_BY_KEY:
                errors.append(_err(field, f"Unknown variable '{{{{{k}}}}}'"))
            elif k not in allowed:
                errors.append(_err(field, f"Variable '{{{{{k}}}}}' is not allowed for purpose '{purpose}'"))

    # Header
    header_keys: List[str] = []
    if header_format == "TEXT":
        if not header_text.strip():
            errors.append(_err("header_text", "Header text is required when the header format is TEXT"))
        else:
            header_keys = extract_keys(header_text)
            if body_length(header_text, variable_type) > HEADER_MAX:
                errors.append(_err("header_text", f"Header must be at most {HEADER_MAX} characters"))
            if len(PLACEHOLDER_RE.findall(header_text)) > 1:
                errors.append(_err("header_text", "Header may contain at most 1 variable"))
            _check_style("header_text", header_keys, variable_type, errors)
            if variable_type == "NUMBER" and any(k != "1" for k in header_keys if k.isdigit()):
                errors.append(_err("header_text", "The header variable must be {{1}}"))
            _check_keys("header_text", header_keys)
    elif header_format in MEDIA_HEADERS:
        _validate_media("header_media", d.get("header_media"), header_format, errors,
                        for_submit=for_submit)
    elif header_format == "CAROUSEL":
        _validate_carousel(d.get("carousel_cards"), variable_type, errors, for_submit=for_submit)

    # Body
    body_keys = extract_keys(body)
    if not body.strip():
        errors.append(_err("body_text", "Body is required"))
    elif body_length(body, variable_type) > BODY_MAX:
        errors.append(_err("body_text", f"Body must be at most {BODY_MAX} characters"))
    _check_body_shape("body_text", body, errors)
    _check_style("body_text", body_keys, variable_type, errors)
    if variable_type == "NUMBER":
        _check_sequence("body_text", body_keys, errors)
    _check_keys("body_text", body_keys)

    # Footer
    if footer:
        if len(footer) > FOOTER_MAX:
            errors.append(_err("footer_text", f"Footer must be at most {FOOTER_MAX} characters"))
        if PLACEHOLDER_RE.search(footer):
            errors.append(_err("footer_text", "Footer must not contain variables"))

    # Limited Time Offer (Marketing Default only)
    lto = d.get("limited_time_offer")
    if lto:
        if not isinstance(lto, dict):
            errors.append(_err("limited_time_offer", "Must be an object"))
        else:
            if category != "MARKETING" or template_type != "DEFAULT":
                errors.append(_err("limited_time_offer",
                                   "Limited Time Offer is only available for Marketing Default templates"))
            if header_format in ("LOCATION", "CAROUSEL"):
                errors.append(_err("limited_time_offer",
                                   "Limited Time Offer needs a text, image, video, document or no header"))
            text = _s(lto.get("text")).strip()
            if not text:
                errors.append(_err("limited_time_offer.text", "Offer text is required"))
            elif len(text) > LTO_TEXT_MAX:
                errors.append(_err("limited_time_offer.text",
                                   f"Offer text must be at most {LTO_TEXT_MAX} characters"))
            if lto.get("expires_at") not in (None, "") and not _parse_date(lto.get("expires_at")):
                errors.append(_err("limited_time_offer.expires_at", "Use an ISO date, e.g. 2026-09-30"))

    # Buttons
    _validate_buttons(d.get("buttons") if d.get("buttons") is not None else [], category,
                      template_type, variable_type, errors, for_submit=for_submit)

    # Sample values (submit only)
    if for_submit:
        for k in header_keys:
            if not _s(header_samples.get(k) or samples.get(k)).strip():
                errors.append(_err("header_sample_values", f"Sample value required for '{{{{{k}}}}}'"))
        for k in body_keys:
            if k in header_keys and variable_type == "NAME":
                continue
            if not _s(samples.get(k)).strip():
                errors.append(_err("sample_values", f"Sample value required for '{{{{{k}}}}}'"))
    return errors


# ---------------------------------------------------------------------------
# Warnings (never block)
# ---------------------------------------------------------------------------
def is_placeholder_url(url: Optional[str]) -> bool:
    host = (urlparse(_s(url).strip()).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in PLACEHOLDER_HOSTS)


def template_warnings(data: Dict[str, Any]) -> List[Dict[str, str]]:
    d = normalise(data)
    warnings: List[Dict[str, str]] = []
    for i, b in enumerate(d.get("buttons") or []):
        if isinstance(b, dict) and b.get("type") == "URL" and is_placeholder_url(b.get("url")):
            warnings.append(_err(f"buttons[{i}].url", PLACEHOLDER_URL_WARNING))
    for i, c in enumerate(d.get("carousel_cards") or []):
        for j, b in enumerate((c or {}).get("buttons") or []):
            if isinstance(b, dict) and b.get("type") == "URL" and is_placeholder_url(b.get("url")):
                warnings.append(_err(f"carousel_cards[{i}].buttons[{j}].url", PLACEHOLDER_URL_WARNING))
    return warnings
