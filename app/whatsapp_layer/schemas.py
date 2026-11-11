"""Pydantic request bodies for the WhatsApp / clawback-notification APIs.

Responses are plain dicts shaped exactly like the contract objects
(serialisers live next to the models' services). Field-level business
validation (template rules, rule overlap, ...) happens in the services and
is reported as 422 `[{field, message}]`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Super admins name the target org in the body (or the query string).
    organization_id: Optional[int] = None


# --- Accounts (§2.1) --------------------------------------------------------
class AccountCreate(_Body):
    provider: str = "omnichannel"
    display_name: str = Field(..., min_length=1, max_length=255)
    base_url: str = Field(..., min_length=1, max_length=512)
    username: Optional[str] = Field(None, max_length=255)
    # Optional so the webhook URL can be generated (and given to the provider)
    # before the provider has issued an access token.
    access_token: Optional[str] = None
    waba_id: Optional[str] = Field(None, max_length=64)
    phone_number_id: Optional[str] = Field(None, max_length=64)
    display_phone_number: Optional[str] = Field(None, max_length=32)


class AccountUpdate(_Body):
    provider: Optional[str] = None
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    base_url: Optional[str] = Field(None, min_length=1, max_length=512)
    username: Optional[str] = Field(None, max_length=255)
    access_token: Optional[str] = None
    waba_id: Optional[str] = Field(None, max_length=64)
    phone_number_id: Optional[str] = Field(None, max_length=64)
    display_phone_number: Optional[str] = Field(None, max_length=32)
    status: Optional[str] = None  # active | disabled


class AccountTest(_Body):
    template_id: Optional[int] = None
    phone_country_code: Optional[str] = None
    phone_number: Optional[str] = None


class PhoneTest(_Body):
    phone_country_code: Optional[str] = Field(None, max_length=8)
    phone_number: str = Field(..., min_length=4, max_length=20)
    template_id: Optional[int] = None
    user_id: Optional[int] = None
    recipient_name: Optional[str] = Field(None, max_length=255)


# --- Templates (§2.2) -------------------------------------------------------
class TemplateButton(BaseModel):
    """One button. `type` decides which of the optional fields apply
    (docs/TEMPLATES_CONTRACT.md §4.6). `text` is accepted as an alias of `label`."""
    model_config = ConfigDict(extra="ignore")
    type: str
    label: Optional[str] = None
    text: Optional[str] = None
    url: Optional[str] = None                 # URL
    url_example: Optional[str] = None         # URL ending in a variable: its sample
    phone_number: Optional[str] = None        # PHONE_NUMBER, e.g. +911234567890
    example: Optional[str] = None             # COPY_CODE: the offer code
    flow_id: Optional[str] = None             # FLOW
    flow_name: Optional[str] = None
    flow_action: Optional[str] = None         # navigate | data_exchange
    navigate_screen: Optional[str] = None


class TemplateMedia(BaseModel):
    """An uploaded file (`s3_key` from POST /templates/media) or a public URL."""
    model_config = ConfigDict(extra="ignore")
    s3_key: Optional[str] = None
    url: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    media_type: Optional[str] = None          # IMAGE | VIDEO | DOCUMENT


class LimitedTimeOffer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: Optional[str] = None
    expires_at: Optional[str] = None          # ISO date/datetime (preview)
    has_expiration: bool = False


class CarouselCard(BaseModel):
    model_config = ConfigDict(extra="ignore")
    media: Optional[TemplateMedia] = None
    body_text: Optional[str] = None
    sample_values: Optional[Dict[str, Any]] = None
    buttons: Optional[List[TemplateButton]] = None


class AuthApp(BaseModel):
    model_config = ConfigDict(extra="ignore")
    package_name: Optional[str] = None
    signature_hash: Optional[str] = None


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code_delivery: Optional[str] = None       # ZERO_TAP | ONE_TAP | COPY_CODE
    apps: Optional[List[AuthApp]] = None
    add_security_recommendation: bool = False
    code_expiration_minutes: Optional[int] = None
    button_text: Optional[str] = None
    autofill_text: Optional[str] = None


class TemplateWrite(_Body):
    name: str
    language: str = "en_US"
    category: str = "UTILITY"
    # DEFAULT | CATALOGUE | CALLING_PERMISSION_REQUEST | OTP; omitted = DEFAULT (OTP for AUTHENTICATION)
    template_type: Optional[str] = None
    purpose: str = "generic"
    # NUMBER | NAME; omitted = worked out from the placeholders used.
    variable_type: Optional[str] = None
    header_format: str = "NONE"
    header_text: Optional[str] = None
    header_media: Optional[TemplateMedia] = None
    header_sample_values: Optional[Dict[str, Any]] = None
    # Required except for AUTHENTICATION (Meta fixes that body).
    body_text: Optional[str] = None
    footer_text: Optional[str] = None
    buttons: Optional[List[TemplateButton]] = None
    sample_values: Optional[Dict[str, Any]] = None
    limited_time_offer: Optional[LimitedTimeOffer] = None
    carousel_cards: Optional[List[CarouselCard]] = None
    auth_config: Optional[AuthConfig] = None
    custom_validity_period: bool = False
    validity_period_seconds: Optional[int] = None
    # Create only: the WhatsApp account the template belongs to (channels
    # contract §5.1); omitted = the org's clawback account. Ignored on update.
    account_id: Optional[int] = None


class TemplateApprove(_Body):
    reason: str = Field(..., min_length=1, max_length=500)


class TemplatePreview(_Body):
    values: Optional[Dict[str, Any]] = None


class TemplateDraftPreview(TemplateWrite):
    """POST /templates/preview: an unsaved editor payload plus optional values."""
    name: str = "preview"
    values: Optional[Dict[str, Any]] = None


# --- Settings (§2.5) --------------------------------------------------------
class SettingsUpdate(_Body):
    enabled: Optional[bool] = None
    timezone: Optional[str] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    working_days: Optional[List[str]] = None
    recruiter_batch_threshold: Optional[int] = Field(None, ge=0, le=1000)
    missed_check_cutoff_time: Optional[str] = None
    fallback_to_created_by: Optional[bool] = None
    digest_skip_if_empty: Optional[bool] = None
    app_base_url: Optional[str] = Field(None, max_length=512)


# --- Rules (§2.6 / §2.7) ----------------------------------------------------
class RuleWrite(_Body):
    name: str = Field(..., min_length=1, max_length=255)
    stage_label: Optional[str] = Field(None, max_length=255)
    action_text: Optional[str] = Field(None, max_length=120)
    anchor: str
    start_day: int
    end_day: Optional[int] = None
    frequency_type: str
    frequency_value: Optional[Dict[str, Any]] = None
    send_time: str = "10:00"
    recipient_type: str = "recruiter"
    template_id: Optional[int] = None
    creates_health_check: bool = True
    priority: int = 0
    is_active: bool = True


class DigestRuleWrite(_Body):
    name: str = Field(..., min_length=1, max_length=255)
    audience_type: str
    audience_value: Optional[Dict[str, Any]] = None
    scope: str = "full_subtree"
    frequency_type: str = "daily"
    frequency_value: Optional[Dict[str, Any]] = None
    send_time: str = "09:30"
    template_id: Optional[int] = None
    sections: Optional[List[str]] = None
    skip_if_empty: Optional[bool] = None
    priority: int = 0
    is_active: bool = True


class Toggle(_Body):
    is_active: bool


class EventToggle(_Body):
    """PATCH /events/{key} (contract §6.2)."""
    enabled: bool


# --- Logs / send now / health checks (§2.8 / §2.9) --------------------------
class SendNow(_Body):
    candidate_job_id: int


class TodayNotify(_Body):
    recruiter_user_id: int


class TodayMessage(_Body):
    recruiter_user_id: int
    message: str = Field(..., min_length=1, max_length=500)


class DashboardRecords(_Body):
    ids: List[int] = Field(default_factory=list, max_length=10000)
    group_by: str = "candidate"
    search: Optional[str] = Field(None, max_length=200)
    statuses: List[str] = Field(default_factory=list)
    company: List[str] = Field(default_factory=list)
    team: List[str] = Field(default_factory=list)
    recruiter: List[str] = Field(default_factory=list)
    job: List[str] = Field(default_factory=list)
    sort: Optional[str] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(10, ge=1, le=100)


class HealthCheckRecord(_Body):
    outcome: str
    notes: Optional[str] = Field(None, max_length=5000)
