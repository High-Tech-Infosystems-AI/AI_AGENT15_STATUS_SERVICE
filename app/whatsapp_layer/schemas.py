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
    access_token: str = Field(..., min_length=1)
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


# --- Templates (§2.2) -------------------------------------------------------
class TemplateButton(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    label: str
    url: Optional[str] = None


class TemplateWrite(_Body):
    name: str
    language: str = "en_US"
    category: str = "UTILITY"
    purpose: str = "generic"
    header_format: str = "NONE"
    header_text: Optional[str] = None
    body_text: str
    footer_text: Optional[str] = None
    buttons: Optional[List[TemplateButton]] = None
    sample_values: Optional[Dict[str, Any]] = None


class TemplateApprove(_Body):
    reason: str = Field(..., min_length=1, max_length=500)


class TemplatePreview(_Body):
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


class HealthCheckRecord(_Body):
    outcome: str
    notes: Optional[str] = Field(None, max_length=5000)
