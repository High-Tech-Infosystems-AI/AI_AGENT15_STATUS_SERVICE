"""WhatsApp / clawback SQLAlchemy ORM models.

Mirrors migrations/v26_whatsapp_clawback.sql. MySQL ENUM columns are mapped
as String here (the DB enforces the enum; the API validates values) and JSON
uses the generic type so the pure-SQLite unit tests can create the tables.
"""
import logging

from sqlalchemy import (
    JSON, BigInteger, Column, Date, DateTime, Index, Integer, LargeBinary,
    String, Text, UniqueConstraint, func,
)

from app.database_Layer.db_config import Base

logger = logging.getLogger("app_logger")

# MySQL BIGINT AUTO_INCREMENT; SQLite only auto-increments INTEGER PKs.
_BigId = BigInteger().with_variant(Integer, "sqlite")


class WhatsAppProviderAccount(Base):
    __tablename__ = "whatsapp_provider_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False)
    provider = Column(String(32), nullable=False, server_default="omnichannel")
    display_name = Column(String(255), nullable=False)
    base_url = Column(String(512), nullable=False)
    username = Column(String(255), nullable=True)
    access_token_enc = Column(LargeBinary, nullable=True)
    token_last4 = Column(String(4), nullable=True)
    waba_id = Column(String(64), nullable=True)
    phone_number_id = Column(String(64), nullable=True)
    display_phone_number = Column(String(32), nullable=True)
    webhook_token = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, server_default="active")  # active | auth_error | disabled
    quality_rating = Column(String(20), nullable=True)
    messaging_limit = Column(String(32), nullable=True)
    sandbox = Column(Integer, nullable=False, server_default="0")
    last_health_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    created_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "provider", name="uq_wa_acct_org_provider"),
        UniqueConstraint("webhook_token", name="uq_wa_acct_webhook_token"),
        Index("idx_wa_acct_status", "status"),
    )


class WhatsAppTemplate(Base):
    __tablename__ = "whatsapp_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False)
    account_id = Column(Integer, nullable=True)
    name = Column(String(512), nullable=False)
    language = Column(String(16), nullable=False, server_default="en_US")
    category = Column(String(20), nullable=False, server_default="UTILITY")
    purpose = Column(String(32), nullable=False, server_default="generic")
    header_format = Column(String(10), nullable=False, server_default="NONE")
    header_text = Column(String(255), nullable=True)
    body_text_named = Column(Text, nullable=False)
    body_text_positional = Column(Text, nullable=True)
    footer_text = Column(String(255), nullable=True)
    buttons = Column(JSON, nullable=True)
    # {"header": ["key"], "body": ["key1", "key2", ...]} — index+1 = position
    variable_map = Column(JSON, nullable=True)
    sample_values = Column(JSON, nullable=True)
    provider_template_id = Column(String(64), nullable=True)
    approval_status = Column(String(20), nullable=False, server_default="draft")
    rejection_reason = Column(Text, nullable=True)
    previous_category = Column(String(20), nullable=True)
    approval_override_by = Column(Integer, nullable=True)
    approval_override_reason = Column(String(500), nullable=True)
    approval_override_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    is_active = Column(Integer, nullable=False, server_default="1")
    version = Column(Integer, nullable=False, server_default="1")
    base_name = Column(String(512), nullable=True)
    parent_template_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    created_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Integer, nullable=True)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_wa_tpl_org_purpose", "organization_id", "purpose", "approval_status"),
        Index("idx_wa_tpl_status", "approval_status"),
    )


class ClawbackNotificationSettings(Base):
    __tablename__ = "clawback_notification_settings"

    organization_id = Column(Integer, primary_key=True, autoincrement=False)
    enabled = Column(Integer, nullable=False, server_default="0")
    timezone = Column(String(64), nullable=False, server_default="Asia/Kolkata")
    quiet_hours_start = Column(String(5), nullable=True, server_default="21:00")
    quiet_hours_end = Column(String(5), nullable=True, server_default="08:00")
    working_days = Column(JSON, nullable=True)
    recruiter_batch_threshold = Column(Integer, nullable=False, server_default="1")
    missed_check_cutoff_time = Column(String(5), nullable=False, server_default="23:00")
    fallback_to_created_by = Column(Integer, nullable=False, server_default="1")
    digest_skip_if_empty = Column(Integer, nullable=False, server_default="1")
    app_base_url = Column(String(512), nullable=True)
    defaults_seeded_at = Column(DateTime, nullable=True)
    # system:* event on/off (contract §6.2), e.g. {"recruiter_summary": false}.
    # Missing keys (or NULL) mean enabled.
    event_toggles = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    created_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Integer, nullable=True)


class ClawbackNotificationRule(Base):
    __tablename__ = "clawback_notification_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False)
    name = Column(String(255), nullable=False)
    stage_label = Column(String(255), nullable=True)
    anchor = Column(String(20), nullable=False)  # offer_accepted | doj | cooling_end
    start_day = Column(Integer, nullable=False)
    end_day = Column(Integer, nullable=True)
    frequency_type = Column(String(20), nullable=False)
    frequency_value = Column(JSON, nullable=True)
    send_time = Column(String(5), nullable=False, server_default="10:00")
    recipient_type = Column(String(20), nullable=False, server_default="recruiter")
    template_id = Column(Integer, nullable=True)
    creates_health_check = Column(Integer, nullable=False, server_default="1")
    priority = Column(Integer, nullable=False, server_default="0")
    is_active = Column(Integer, nullable=False, server_default="1")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    created_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Integer, nullable=True)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_cb_rule_org", "organization_id", "is_active", "anchor", "start_day"),
        Index("idx_cb_rule_template", "template_id"),
    )


class ClawbackDigestRule(Base):
    __tablename__ = "clawback_digest_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False)
    name = Column(String(255), nullable=False)
    audience_type = Column(String(32), nullable=False)
    audience_value = Column(JSON, nullable=True)
    scope = Column(String(20), nullable=False, server_default="full_subtree")
    frequency_type = Column(String(20), nullable=False, server_default="daily")
    frequency_value = Column(JSON, nullable=True)
    send_time = Column(String(5), nullable=False, server_default="09:30")
    template_id = Column(Integer, nullable=True)
    sections = Column(JSON, nullable=True)
    skip_if_empty = Column(Integer, nullable=False, server_default="1")
    priority = Column(Integer, nullable=False, server_default="0")
    is_active = Column(Integer, nullable=False, server_default="1")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    created_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Integer, nullable=True)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_cb_digest_org", "organization_id", "is_active"),
        Index("idx_cb_digest_template", "template_id"),
    )


class ClawbackHealthCheck(Base):
    __tablename__ = "clawback_health_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False)
    candidate_job_id = Column(Integer, nullable=False)
    candidate_job_status_id = Column(Integer, nullable=True)
    candidate_id = Column(String(64), nullable=True)
    candidate_name = Column(String(255), nullable=True)
    job_id = Column(Integer, nullable=True)
    job_title = Column(String(255), nullable=True)
    company_name = Column(String(255), nullable=True)
    recruiter_user_id = Column(Integer, nullable=True)
    rule_id = Column(Integer, nullable=True)
    stage_label = Column(String(255), nullable=True)
    anchor = Column(String(20), nullable=True)
    day_number = Column(Integer, nullable=True)
    due_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, server_default="pending")  # pending | done | missed | cancelled
    outcome = Column(String(20), nullable=True)  # engaged | at_risk | not_reachable | dropped
    notes = Column(Text, nullable=True)
    recorded_by = Column(Integer, nullable=True)
    recorded_at = Column(DateTime, nullable=True)
    cancelled_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("candidate_job_id", "due_date", name="uq_cb_hc_cj_due"),
        Index("idx_cb_hc_org_status_due", "organization_id", "status", "due_date"),
        Index("idx_cb_hc_recruiter_due", "recruiter_user_id", "due_date"),
        Index("idx_cb_hc_job", "job_id"),
    )


class WhatsAppMessageLog(Base):
    __tablename__ = "whatsapp_message_logs"

    id = Column(_BigId, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False)
    account_id = Column(Integer, nullable=True)
    provider = Column(String(32), nullable=True)
    kind = Column(String(20), nullable=False)
    rule_id = Column(Integer, nullable=True)
    rule_name = Column(String(255), nullable=True)
    digest_rule_id = Column(Integer, nullable=True)
    template_id = Column(Integer, nullable=True)
    template_name = Column(String(512), nullable=True)
    candidate_job_id = Column(Integer, nullable=True)
    candidate_job_status_id = Column(Integer, nullable=True)
    candidate_id = Column(String(64), nullable=True)
    candidate_name = Column(String(255), nullable=True)
    job_id = Column(Integer, nullable=True)
    job_title = Column(String(255), nullable=True)
    health_check_ids = Column(JSON, nullable=True)
    recipient_user_id = Column(Integer, nullable=True)
    recipient_name = Column(String(255), nullable=True)
    recipient_type = Column(String(20), nullable=True)
    phone_e164 = Column(String(20), nullable=True)
    day_number = Column(Integer, nullable=True)
    scheduled_for = Column(DateTime, nullable=True)
    params = Column(JSON, nullable=True)
    rendered_body = Column(Text, nullable=True)
    digest_snapshot = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, server_default="queued")
    skip_reason = Column(String(32), nullable=True)
    provider_ref = Column(String(64), nullable=True)
    provider_message_id = Column(String(128), nullable=True)
    correlation_id = Column(String(64), nullable=True)
    idempotency_key = Column(String(64), nullable=False)
    error_code = Column(String(32), nullable=True)
    error_message = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, server_default="0")
    next_attempt_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)
    triggered_by = Column(String(20), nullable=False, server_default="scheduler")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_wa_log_idem"),
        Index("idx_wa_log_org_status_sched", "organization_id", "status", "scheduled_for"),
        Index("idx_wa_log_cj", "candidate_job_id"),
        Index("idx_wa_log_cjs", "candidate_job_status_id"),
        Index("idx_wa_log_recipient", "recipient_user_id", "created_at"),
        Index("idx_wa_log_provider_ref", "provider_ref"),
        Index("idx_wa_log_provider_msg", "provider_message_id"),
        Index("idx_wa_log_phone", "phone_e164", "submitted_at"),
        Index("idx_wa_log_org_created", "organization_id", "created_at"),
    )


class WhatsAppWebhookEvent(Base):
    __tablename__ = "whatsapp_webhook_events"

    id = Column(_BigId, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=True)
    organization_id = Column(Integer, nullable=True)
    received_at = Column(DateTime, nullable=False, server_default=func.now())
    headers = Column(JSON, nullable=True)
    body = Column(JSON, nullable=True)
    body_raw = Column(Text, nullable=True)
    processed = Column(Integer, nullable=False, server_default="0")
    matched_log_ids = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_wa_wh_received", "received_at"),
        Index("idx_wa_wh_account", "account_id", "received_at"),
    )


class WhatsAppOptOut(Base):
    __tablename__ = "whatsapp_opt_outs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False)
    phone_e164 = Column(String(20), nullable=False)
    user_id = Column(Integer, nullable=True)
    reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    created_by = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "phone_e164", name="uq_wa_optout_org_phone"),
        Index("idx_wa_optout_user", "user_id"),
    )
