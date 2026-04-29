-- v18_ai_chatbot.sql
-- AI Chatbot ("Ask Your Data") tables.
--
-- Five new tables wired alongside the existing chat:
--   1) ai_token_quota             — per-user daily/monthly LLM token budget
--   2) ai_query_audit             — immutable record of every AI query
--   3) ai_scheduled_query         — recurring user-pinned prompts (cron)
--   4) ai_anomaly_subscription    — built-in metric watchers
--   5) ai_approval                — hierarchical approval queue
--
-- The synthetic "ai_assistant" user (sender_id of every AI reply) is provisioned
-- at service startup the same way status_bot is — no migration needed.

CREATE TABLE IF NOT EXISTS ai_token_quota (
    user_id        INT          NOT NULL PRIMARY KEY,
    daily_limit    INT          NOT NULL DEFAULT 50000,
    monthly_limit  INT          NOT NULL DEFAULT 1000000,
    used_today     INT          NOT NULL DEFAULT 0,
    used_month     INT          NOT NULL DEFAULT 0,
    day_anchor     DATE         NOT NULL,
    month_anchor   CHAR(7)      NOT NULL,
    updated_by     INT          NULL,
    updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_quota_user (user_id)
);

CREATE TABLE IF NOT EXISTS ai_query_audit (
    id              BIGINT       NOT NULL PRIMARY KEY AUTO_INCREMENT,
    user_id         INT          NOT NULL,
    conversation_id INT          NULL,
    prompt          MEDIUMTEXT   NOT NULL,
    refs            JSON         NULL,
    tools_called    JSON         NULL,
    model           VARCHAR(64)  NOT NULL,
    prompt_version  VARCHAR(32)  NOT NULL,
    tokens_in       INT          NOT NULL DEFAULT 0,
    tokens_out      INT          NOT NULL DEFAULT 0,
    latency_ms      INT          NOT NULL DEFAULT 0,
    status          ENUM('ok','error','rejected_quota','rejected_acl') NOT NULL,
    error_msg       VARCHAR(500) NULL,
    ip_address      VARCHAR(64)  NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_audit_user_time (user_id, created_at),
    INDEX idx_audit_status (status)
);

CREATE TABLE IF NOT EXISTS ai_scheduled_query (
    id           BIGINT      NOT NULL PRIMARY KEY AUTO_INCREMENT,
    user_id      INT         NOT NULL,
    name         VARCHAR(120) NOT NULL,
    prompt       MEDIUMTEXT  NOT NULL,
    refs         JSON        NULL,
    cron_expr    VARCHAR(64) NOT NULL,
    timezone     VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    is_active    TINYINT(1)  NOT NULL DEFAULT 0,
    last_run_at  DATETIME    NULL,
    next_run_at  DATETIME    NULL,
    created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_sq_due (is_active, next_run_at),
    INDEX idx_sq_user (user_id)
);

CREATE TABLE IF NOT EXISTS ai_anomaly_subscription (
    id            BIGINT      NOT NULL PRIMARY KEY AUTO_INCREMENT,
    user_id       INT         NOT NULL,
    name          VARCHAR(120) NOT NULL,
    metric_key    VARCHAR(64) NOT NULL,
    params        JSON        NOT NULL,
    is_active     TINYINT(1)  NOT NULL DEFAULT 0,
    cooldown_min  INT         NOT NULL DEFAULT 360,
    last_fired_at DATETIME    NULL,
    created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_an_active (is_active),
    INDEX idx_an_user (user_id)
);

CREATE TABLE IF NOT EXISTS ai_approval (
    id            BIGINT      NOT NULL PRIMARY KEY AUTO_INCREMENT,
    user_id       INT         NOT NULL,
    origin        ENUM('schedule_create','anomaly_create','action_suggest') NOT NULL,
    payload       JSON        NOT NULL,
    status        ENUM('pending','approved','declined','expired') NOT NULL DEFAULT 'pending',
    approver_role ENUM('admin_or_super','super','self') NOT NULL,
    decided_by    INT         NULL,
    decided_at    DATETIME    NULL,
    target_id     BIGINT      NULL,
    target_kind   VARCHAR(32) NULL,
    created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_approval_user_status (user_id, status),
    INDEX idx_approval_role_status (approver_role, status)
);
