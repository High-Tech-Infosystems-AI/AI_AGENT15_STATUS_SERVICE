"""WhatsApp provider interface + the plain dataclasses passed across it.

Callers (engine, API, tasks) only ever talk to this interface, so adding a
provider (Gupshup, ...) means a new class plus a registry entry. Providers
never touch the DB: they get a `ProviderCredentials` with the decrypted
token and return dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class ProviderError(Exception):
    """A provider call failed. `retryable` = 5xx / timeout / network;
    `auth_error` = 401/403 (the account gets marked auth_error)."""

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 retryable: bool = False, auth_error: bool = False,
                 raw: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.auth_error = auth_error
        self.raw = raw


class ProviderUnsupported(ProviderError):
    """The provider has no (known) API for this operation."""


@dataclass
class ProviderCredentials:
    base_url: str
    access_token: str
    username: Optional[str] = None
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None


@dataclass
class TemplateRef:
    name: str
    language: str
    # Set for templates approved with named variables (parameterFormat NAMED):
    # the variable name of each body value, in the same order.
    body_param_names: List[str] = field(default_factory=list)


@dataclass
class TemplateSpec:
    """A template ready for the provider: positional placeholders only.
    Field shapes are documented in docs/TEMPLATES_CONTRACT.md."""
    name: str
    language: str
    category: str = "UTILITY"
    # DEFAULT | CATALOGUE | CALLING_PERMISSION_REQUEST | OTP
    template_type: str = "DEFAULT"
    # NONE | TEXT | IMAGE | VIDEO | DOCUMENT | LOCATION | CAROUSEL
    header_format: str = "NONE"
    header_text: Optional[str] = None
    header_example: List[str] = field(default_factory=list)
    # (file_name, bytes, mime) for IMAGE / VIDEO / DOCUMENT headers
    header_file: Optional[Tuple[str, bytes, str]] = None
    body_text: str = ""
    body_example: List[str] = field(default_factory=list)
    footer_text: Optional[str] = None
    # [{"type": PHONE_NUMBER|URL|VOICE_CALL|COPY_CODE|MPM|FLOW|QUICK_REPLY,
    #   "label": str, "url"?, "example"?, "phone_number"?, "flow_id"?, ...}]
    buttons: List[Dict[str, Any]] = field(default_factory=list)
    # {"text": str, "has_expiration": bool, "expires_at"?: str}
    limited_time_offer: Optional[Dict[str, Any]] = None
    # [{"media_type", "media_url", "body_text", "body_example", "buttons"}]
    carousel_cards: List[Dict[str, Any]] = field(default_factory=list)
    # {"code_delivery", "apps": [{"package_name", "signature_hash"}], ...}
    auth_config: Optional[Dict[str, Any]] = None
    custom_validity_period: bool = False
    time_to_live: Optional[int] = None           # minutes (vendor doc)


@dataclass
class ProviderTemplate:
    name: Optional[str]
    language: Optional[str]
    provider_template_id: Optional[str] = None
    # Normalised to our approval_status vocabulary:
    # pending | approved | rejected | paused | disabled
    status: Optional[str] = None
    category: Optional[str] = None
    previous_category: Optional[str] = None
    rejection_reason: Optional[str] = None
    # Template content, when the provider returns it (used to import).
    header_text: Optional[str] = None
    body_text: Optional[str] = None
    footer_text: Optional[str] = None
    sample_values: Dict[str, str] = field(default_factory=dict)
    parameter_format: Optional[str] = None   # NAMED | POSITIONAL
    template_type: Optional[str] = None      # our vocabulary (DEFAULT, OTP, ...)
    header_format: Optional[str] = None
    header_media: Optional[Dict[str, Any]] = None
    buttons: Optional[List[Dict[str, Any]]] = None
    time_to_live: Optional[int] = None
    raw: Any = None


@dataclass
class SendResult:
    accepted: bool
    provider_ref: Optional[str] = None       # BroadNet batchId
    provider_message_id: Optional[str] = None  # wamid when the provider returns one
    duplicate: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False
    auth_error: bool = False
    raw: Any = None


@dataclass
class SendStatus:
    # pending | sent | failed | unknown
    status: str
    processed: Optional[int] = None
    pending: Optional[int] = None
    error_message: Optional[str] = None
    raw: Any = None


@dataclass
class AccountHealth:
    ok: bool
    status: Optional[str] = None             # provider's own status string (ACTIVE, ...)
    quality_rating: Optional[str] = None
    messaging_limit: Optional[str] = None
    sandbox: Optional[bool] = None
    display_phone_number: Optional[str] = None
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    auth_error: bool = False
    error: Optional[str] = None
    raw: Any = None


@dataclass
class WebhookEvent:
    """One status change understood from a webhook payload."""
    # message_status | template_status | template_quality | template_category
    # | flow_event | inbound
    kind: str
    text: Optional[str] = None                # inbound: button label or message text
    status: Optional[str] = None              # sent|delivered|read|failed (message) / approval status (template)
    provider_message_id: Optional[str] = None
    recipient: Optional[str] = None           # digits, no '+'
    timestamp: Optional[int] = None           # unix seconds
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    batch_id: Optional[str] = None
    campaign_name: Optional[str] = None
    template_name: Optional[str] = None
    template_language: Optional[str] = None
    provider_template_id: Optional[str] = None
    reason: Optional[str] = None
    # Meta's own event word (APPROVED, FLAGGED, REINSTATED, ENDPOINT_AVAILABILITY, ...)
    event: Optional[str] = None
    # template_quality: GREEN | YELLOW | RED | UNKNOWN
    quality_score: Optional[str] = None
    previous_quality_score: Optional[str] = None
    # template_category
    category: Optional[str] = None
    previous_category: Optional[str] = None
    # flow_event
    flow_id: Optional[str] = None
    alert_state: Optional[str] = None
    raw: Any = None


class WhatsAppProvider:
    """Base class — every provider implements all of these."""

    key: str = ""
    label: str = ""

    def send_template(self, creds: ProviderCredentials, to_digits: str,
                      template: TemplateRef, header_vars: List[str],
                      body_vars: List[str], correlation_id: str) -> SendResult:
        raise NotImplementedError

    def create_template(self, creds: ProviderCredentials,
                        spec: TemplateSpec) -> ProviderTemplate:
        raise NotImplementedError

    def get_template(self, creds: ProviderCredentials, name: str,
                     language: str) -> Optional[ProviderTemplate]:
        raise NotImplementedError

    def list_templates(self, creds: ProviderCredentials,
                       category: Optional[str] = None) -> List[ProviderTemplate]:
        raise NotImplementedError

    def delete_template(self, creds: ProviderCredentials, name: str) -> None:
        raise NotImplementedError

    def send_text(self, creds: ProviderCredentials, to_digits: str, text: str,
                  correlation_id: str) -> SendResult:
        """Free-form text; only delivered within 24 hours of the person's last message."""
        raise NotImplementedError

    def get_send_status(self, creds: ProviderCredentials,
                        provider_ref: str) -> SendStatus:
        raise NotImplementedError

    def parse_webhook(self, headers: Dict[str, str],
                      body: Any) -> List[WebhookEvent]:
        raise NotImplementedError

    def health_check(self, creds: ProviderCredentials) -> AccountHealth:
        raise NotImplementedError
