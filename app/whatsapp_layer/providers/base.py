"""WhatsApp provider interface + the plain dataclasses passed across it.

Callers (engine, API, tasks) only ever talk to this interface, so adding a
provider (Gupshup, ...) means a new class plus a registry entry. Providers
never touch the DB: they get a `ProviderCredentials` with the decrypted
token and return dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
    """A template ready for the provider: positional placeholders only."""
    name: str
    language: str
    category: str = "UTILITY"
    header_format: str = "NONE"
    header_text: Optional[str] = None
    header_example: List[str] = field(default_factory=list)
    body_text: str = ""
    body_example: List[str] = field(default_factory=list)
    footer_text: Optional[str] = None
    # [{"type": "URL"|"QUICK_REPLY", "label": str, "url"?: str}]
    buttons: List[Dict[str, Any]] = field(default_factory=list)


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
    kind: str                                 # message_status | template_status
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

    def get_send_status(self, creds: ProviderCredentials,
                        provider_ref: str) -> SendStatus:
        raise NotImplementedError

    def parse_webhook(self, headers: Dict[str, str],
                      body: Any) -> List[WebhookEvent]:
        raise NotImplementedError

    def health_check(self, creds: ProviderCredentials) -> AccountHealth:
        raise NotImplementedError
