-- v26_whatsapp_clawback.sql
-- Clawback WhatsApp notifications — provider accounts, WhatsApp templates,
-- recruiter reminder rules, team digest rules, clawback health checks,
-- message logs, raw webhook events and opt-outs.
--
-- Design:   CLAWBACK_WHATSAPP_NOTIFICATIONS_PLAN.md §5
-- Contract: CLAWBACK_WHATSAPP_API_CONTRACT.md (wins over the plan). Every
--           table that references a clawback keys on candidate_job_id
--           (NOT NULL) and carries candidate_job_status_id as a nullable
--           extra, because pre-joining candidates have no
--           candidate_job_status row yet.
--
-- Idempotent: every statement is CREATE TABLE IF NOT EXISTS, safe to re-run.
-- Run on ats_staging first, then ats_basic (prod).

-- ---------------------------------------------------------------------------
-- 1. whatsapp_provider_accounts — one account per org per provider. The
--    access token is Fernet ciphertext (key = WHATSAPP_CRED_KEY, a K8s
--    secret) and is never returned by the API. webhook_token is the random
--    path segment of the account's webhook URL.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whatsapp_provider_accounts (
    id                    INT NOT NULL AUTO_INCREMENT,
    organization_id       INT NOT NULL,
    provider              ENUM('omnichannel') NOT NULL DEFAULT 'omnichannel',
    display_name          VARCHAR(255) NOT NULL,
    base_url              VARCHAR(512) NOT NULL,
    username              VARCHAR(255) NULL,
    access_token_enc      BLOB NULL,
    token_last4           VARCHAR(4) NULL,
    waba_id               VARCHAR(64) NULL,
    phone_number_id       VARCHAR(64) NULL,
    display_phone_number  VARCHAR(32) NULL,
    webhook_token         VARCHAR(64) NOT NULL,
    status                ENUM('active','auth_error','disabled') NOT NULL DEFAULT 'active',
    quality_rating        VARCHAR(20) NULL,
    messaging_limit       VARCHAR(32) NULL,
    sandbox               TINYINT(1) NOT NULL DEFAULT 0,
    last_health_at        DATETIME NULL,
    last_error            TEXT NULL,
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by            INT NULL,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by            INT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_wa_acct_org_provider (organization_id, provider),
    UNIQUE KEY uq_wa_acct_webhook_token (webhook_token),
    KEY idx_wa_acct_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 2. whatsapp_templates — admin-authored templates. The editor works with
--    named variables ({{candidate_name}}); on submit they are converted to
--    positional ({{1}}) and variable_map records the order.
--    Uniqueness of (account, name, language) among non-deleted rows is
--    enforced by the API (soft-deleted rows would otherwise block re-use).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whatsapp_templates (
    id                        INT NOT NULL AUTO_INCREMENT,
    organization_id           INT NOT NULL,
    account_id                INT NULL,
    name                      VARCHAR(512) NOT NULL,
    language                  VARCHAR(16) NOT NULL DEFAULT 'en_US',
    category                  ENUM('UTILITY','MARKETING') NOT NULL DEFAULT 'UTILITY',
    purpose                   ENUM('recruiter_reminder','recruiter_batch','final_validation','team_digest','generic') NOT NULL DEFAULT 'generic',
    header_format             ENUM('NONE','TEXT') NOT NULL DEFAULT 'NONE',
    header_text               VARCHAR(255) NULL,
    body_text_named           TEXT NOT NULL,
    body_text_positional      TEXT NULL,
    footer_text               VARCHAR(255) NULL,
    buttons                   JSON NULL,
    variable_map              JSON NULL,
    sample_values             JSON NULL,
    provider_template_id      VARCHAR(64) NULL,
    approval_status           ENUM('draft','pending','approved','rejected','paused','disabled') NOT NULL DEFAULT 'draft',
    rejection_reason          TEXT NULL,
    previous_category         VARCHAR(20) NULL,
    approval_override_by      INT NULL,
    approval_override_reason  VARCHAR(500) NULL,
    approval_override_at      DATETIME NULL,
    submitted_at              DATETIME NULL,
    last_synced_at            DATETIME NULL,
    is_active                 TINYINT(1) NOT NULL DEFAULT 1,
    version                   INT NOT NULL DEFAULT 1,
    base_name                 VARCHAR(512) NULL,
    parent_template_id        INT NULL,
    created_at                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by                INT NULL,
    updated_at                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by                INT NULL,
    deleted_at                DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_wa_tpl_org_purpose (organization_id, purpose, approval_status),
    KEY idx_wa_tpl_acct_name (account_id, name(191), language),
    KEY idx_wa_tpl_status (approval_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 3. clawback_notification_settings — one row per org (created with defaults
--    on first read). Times are 'HH:MM' in the org timezone.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clawback_notification_settings (
    organization_id            INT NOT NULL,
    enabled                    TINYINT(1) NOT NULL DEFAULT 0,
    timezone                   VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    quiet_hours_start          VARCHAR(5) NULL DEFAULT '21:00',
    quiet_hours_end            VARCHAR(5) NULL DEFAULT '08:00',
    working_days               JSON NULL,
    recruiter_batch_threshold  INT NOT NULL DEFAULT 3,
    missed_check_cutoff_time   VARCHAR(5) NOT NULL DEFAULT '23:00',
    fallback_to_created_by     TINYINT(1) NOT NULL DEFAULT 1,
    digest_skip_if_empty       TINYINT(1) NOT NULL DEFAULT 1,
    app_base_url               VARCHAR(512) NULL,
    defaults_seeded_at         DATETIME NULL,
    created_at                 DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by                 INT NULL,
    updated_at                 DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by                 INT NULL,
    PRIMARY KEY (organization_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 4. clawback_notification_rules — recruiter reminder matrix. Active rules
--    with the same anchor must not share a day (validated by the API).
--    end_day NULL = open-ended.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clawback_notification_rules (
    id                    INT NOT NULL AUTO_INCREMENT,
    organization_id       INT NOT NULL,
    name                  VARCHAR(255) NOT NULL,
    stage_label           VARCHAR(255) NULL,
    anchor                ENUM('offer_accepted','doj','cooling_end') NOT NULL,
    start_day             INT NOT NULL,
    end_day               INT NULL,
    frequency_type        ENUM('every_n_days','specific_days','weekly','weekdays','once') NOT NULL,
    frequency_value       JSON NULL,
    send_time             VARCHAR(5) NOT NULL DEFAULT '10:00',
    recipient_type        ENUM('recruiter') NOT NULL DEFAULT 'recruiter',
    template_id           INT NULL,
    creates_health_check  TINYINT(1) NOT NULL DEFAULT 1,
    priority              INT NOT NULL DEFAULT 0,
    is_active             TINYINT(1) NOT NULL DEFAULT 1,
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by            INT NULL,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by            INT NULL,
    deleted_at            DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_cb_rule_org (organization_id, is_active, anchor, start_day),
    KEY idx_cb_rule_template (template_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 5. clawback_digest_rules — consolidated summaries for TLs and above.
--    A person matching several rules on the same day gets only the
--    highest-priority one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clawback_digest_rules (
    id               INT NOT NULL AUTO_INCREMENT,
    organization_id  INT NOT NULL,
    name             VARCHAR(255) NOT NULL,
    audience_type    ENUM('team_managers','has_subordinate_managers','designation_levels','roles','users') NOT NULL,
    audience_value   JSON NULL,
    scope            ENUM('direct_team','full_subtree') NOT NULL DEFAULT 'full_subtree',
    frequency_type   ENUM('daily','weekly','weekdays') NOT NULL DEFAULT 'daily',
    frequency_value  JSON NULL,
    send_time        VARCHAR(5) NOT NULL DEFAULT '09:30',
    template_id      INT NULL,
    sections         JSON NULL,
    skip_if_empty    TINYINT(1) NOT NULL DEFAULT 1,
    priority         INT NOT NULL DEFAULT 0,
    is_active        TINYINT(1) NOT NULL DEFAULT 1,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by       INT NULL,
    updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by       INT NULL,
    deleted_at       DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_cb_digest_org (organization_id, is_active),
    KEY idx_cb_digest_template (template_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 6. clawback_health_checks — one per candidate_job per day. Candidate / job
--    names are snapshotted because the clawback lives in the job service.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clawback_health_checks (
    id                       INT NOT NULL AUTO_INCREMENT,
    organization_id          INT NOT NULL,
    candidate_job_id         INT NOT NULL,
    candidate_job_status_id  INT NULL,
    candidate_id             VARCHAR(64) NULL,
    candidate_name           VARCHAR(255) NULL,
    job_id                   INT NULL,
    job_title                VARCHAR(255) NULL,
    company_name             VARCHAR(255) NULL,
    recruiter_user_id        INT NULL,
    rule_id                  INT NULL,
    stage_label              VARCHAR(255) NULL,
    anchor                   ENUM('offer_accepted','doj','cooling_end') NULL,
    day_number               INT NULL,
    due_date                 DATE NOT NULL,
    status                   ENUM('pending','done','missed','cancelled') NOT NULL DEFAULT 'pending',
    outcome                  ENUM('engaged','at_risk','not_reachable','dropped') NULL,
    notes                    TEXT NULL,
    recorded_by              INT NULL,
    recorded_at              DATETIME NULL,
    cancelled_reason         VARCHAR(64) NULL,
    created_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_cb_hc_cj_due (candidate_job_id, due_date),
    KEY idx_cb_hc_org_status_due (organization_id, status, due_date),
    KEY idx_cb_hc_recruiter_due (recruiter_user_id, due_date),
    KEY idx_cb_hc_job (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 7. whatsapp_message_logs — every send attempt and every skip.
--    idempotency_key = sha1(kind, rule/digest id, subject id, recipient,
--    local date); the UNIQUE key makes scheduler re-runs safe.
--    provider_ref = BroadNet batchId; correlation_id = campaignName.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whatsapp_message_logs (
    id                       BIGINT NOT NULL AUTO_INCREMENT,
    organization_id          INT NOT NULL,
    account_id               INT NULL,
    provider                 VARCHAR(32) NULL,
    kind                     ENUM('reminder','batch_reminder','final_validation','digest','manual','test') NOT NULL,
    rule_id                  INT NULL,
    rule_name                VARCHAR(255) NULL,
    digest_rule_id           INT NULL,
    template_id              INT NULL,
    template_name            VARCHAR(512) NULL,
    candidate_job_id         INT NULL,
    candidate_job_status_id  INT NULL,
    candidate_id             VARCHAR(64) NULL,
    candidate_name           VARCHAR(255) NULL,
    job_id                   INT NULL,
    job_title                VARCHAR(255) NULL,
    health_check_ids         JSON NULL,
    recipient_user_id        INT NULL,
    recipient_name           VARCHAR(255) NULL,
    recipient_type           VARCHAR(20) NULL,
    phone_e164               VARCHAR(20) NULL,
    day_number               INT NULL,
    scheduled_for            DATETIME NULL,
    params                   JSON NULL,
    rendered_body            TEXT NULL,
    digest_snapshot          JSON NULL,
    status                   ENUM('queued','submitted','sent','delivered','read','failed','skipped') NOT NULL DEFAULT 'queued',
    skip_reason              VARCHAR(32) NULL,
    provider_ref             VARCHAR(64) NULL,
    provider_message_id      VARCHAR(128) NULL,
    correlation_id           VARCHAR(64) NULL,
    idempotency_key          VARCHAR(64) NOT NULL,
    error_code               VARCHAR(32) NULL,
    error_message            TEXT NULL,
    attempts                 INT NOT NULL DEFAULT 0,
    next_attempt_at          DATETIME NULL,
    submitted_at             DATETIME NULL,
    sent_at                  DATETIME NULL,
    delivered_at             DATETIME NULL,
    read_at                  DATETIME NULL,
    failed_at                DATETIME NULL,
    triggered_by             ENUM('scheduler','manual','retry') NOT NULL DEFAULT 'scheduler',
    created_by               INT NULL,
    created_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_wa_log_idem (idempotency_key),
    KEY idx_wa_log_org_status_sched (organization_id, status, scheduled_for),
    KEY idx_wa_log_cj (candidate_job_id),
    KEY idx_wa_log_cjs (candidate_job_status_id),
    KEY idx_wa_log_recipient (recipient_user_id, created_at),
    KEY idx_wa_log_provider_ref (provider_ref),
    KEY idx_wa_log_provider_msg (provider_message_id),
    KEY idx_wa_log_phone (phone_e164, submitted_at),
    KEY idx_wa_log_org_created (organization_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 8. whatsapp_webhook_events — raw inbound payloads, kept 30 days (cleanup
--    task). Stored before parsing so nothing is lost while the vendor
--    payload format is unconfirmed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whatsapp_webhook_events (
    id               BIGINT NOT NULL AUTO_INCREMENT,
    account_id       INT NULL,
    organization_id  INT NULL,
    received_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    headers          JSON NULL,
    body             JSON NULL,
    body_raw         MEDIUMTEXT NULL,
    processed        TINYINT(1) NOT NULL DEFAULT 0,
    matched_log_ids  JSON NULL,
    error            TEXT NULL,
    PRIMARY KEY (id),
    KEY idx_wa_wh_received (received_at),
    KEY idx_wa_wh_account (account_id, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 9. whatsapp_opt_outs — numbers that must not receive messages.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whatsapp_opt_outs (
    id               INT NOT NULL AUTO_INCREMENT,
    organization_id  INT NOT NULL,
    phone_e164       VARCHAR(20) NOT NULL,
    user_id          INT NULL,
    reason           VARCHAR(255) NULL,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by       INT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_wa_optout_org_phone (organization_id, phone_e164),
    KEY idx_wa_optout_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
