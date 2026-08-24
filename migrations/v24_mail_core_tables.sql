-- v24_mail_core_tables.sql
-- Mail / Mailbox Service — per-user SMTP+IMAP mailboxes with Outlook-style
-- folders, messages, attachments and settings. Hosted inside the Status
-- Service as a separate FastAPI app (app.mail_main:app, port 8521, path /mail).
--
-- v1 scope: per-user only. No admin / super-admin cross-mailbox access.
-- Run on ats_staging first, then ats_basic (prod).

-- ---------------------------------------------------------------------------
-- 1. mail_accounts — one connected mailbox per user (v1). Credentials stored
--    as Fernet ciphertext (key = MAIL_ENCRYPTION_KEY, a K8s secret).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_accounts (
    id                    INT NOT NULL AUTO_INCREMENT,
    user_id               INT NOT NULL,
    organization_id       INT NULL,
    display_name          VARCHAR(255) NOT NULL,
    email_address         VARCHAR(320) NOT NULL,
    provider              VARCHAR(20)  NOT NULL DEFAULT 'custom',
    -- outgoing (SMTP)
    smtp_host             VARCHAR(255) NOT NULL,
    smtp_port             INT NOT NULL DEFAULT 587,
    smtp_security         VARCHAR(10)  NOT NULL DEFAULT 'starttls',
    smtp_username         VARCHAR(320) NOT NULL,
    smtp_password_enc     BLOB NOT NULL,
    -- incoming (IMAP)
    imap_host             VARCHAR(255) NOT NULL,
    imap_port             INT NOT NULL DEFAULT 993,
    imap_security         VARCHAR(10)  NOT NULL DEFAULT 'ssl',
    imap_username         VARCHAR(320) NOT NULL,
    imap_password_enc     BLOB NULL,
    use_same_credentials  TINYINT(1) NOT NULL DEFAULT 1,
    -- sync
    sync_enabled          TINYINT(1) NOT NULL DEFAULT 1,
    sync_interval_seconds INT NOT NULL DEFAULT 300,
    backfill_days         INT NOT NULL DEFAULT 90,
    use_idle              TINYINT(1) NOT NULL DEFAULT 0,
    -- status
    status                VARCHAR(20) NOT NULL DEFAULT 'pending',
    last_error            TEXT NULL,
    last_synced_at        DATETIME NULL,
    consent_acknowledged  TINYINT(1) NOT NULL DEFAULT 0,
    is_default            TINYINT(1) NOT NULL DEFAULT 1,
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted_at            DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_user_email (user_id, email_address),
    KEY idx_mail_acct_user (user_id),
    KEY idx_mail_acct_org (organization_id),
    KEY idx_mail_acct_sync (status, sync_enabled, last_synced_at),
    CONSTRAINT fk_mail_acct_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 2. mail_folders — Outlook system folders + custom, one IMAP cursor each.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_folders (
    id                INT NOT NULL AUTO_INCREMENT,
    account_id        INT NOT NULL,
    user_id           INT NOT NULL,
    organization_id   INT NULL,
    name              VARCHAR(255) NOT NULL,
    role              VARCHAR(12) NOT NULL DEFAULT 'custom',
    imap_path         VARCHAR(512) NULL,
    parent_folder_id  INT NULL,
    uidvalidity       BIGINT NULL,
    last_uid          BIGINT NULL,
    unread_count      INT NOT NULL DEFAULT 0,
    total_count       INT NOT NULL DEFAULT 0,
    sort_order        INT NOT NULL DEFAULT 0,
    is_system         TINYINT(1) NOT NULL DEFAULT 0,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at        DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_acct_role_sys (account_id, role, is_system),
    KEY idx_mail_fold_acct (account_id),
    KEY idx_mail_fold_user (user_id),
    CONSTRAINT fk_mail_fold_acct FOREIGN KEY (account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE,
    CONSTRAINT fk_mail_fold_parent FOREIGN KEY (parent_folder_id) REFERENCES mail_folders(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 3. mail_messages — the high-volume table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_messages (
    id                BIGINT NOT NULL AUTO_INCREMENT,
    account_id        INT NOT NULL,
    folder_id         INT NOT NULL,
    user_id           INT NOT NULL,
    organization_id   INT NULL,
    imap_uid          BIGINT NULL,
    message_id_hdr    VARCHAR(512) NULL,
    conversation_id   VARCHAR(255) NULL,
    in_reply_to       VARCHAR(512) NULL,
    references_hdr    TEXT NULL,
    direction         VARCHAR(3) NOT NULL,
    from_name         VARCHAR(255) NULL,
    from_address      VARCHAR(320) NULL,
    to_json           JSON NULL,
    cc_json           JSON NULL,
    bcc_json          JSON NULL,
    reply_to_address  VARCHAR(320) NULL,
    subject           VARCHAR(998) NULL,
    snippet           VARCHAR(512) NULL,
    body_text         MEDIUMTEXT NULL,
    body_html         MEDIUMTEXT NULL,
    has_attachments   TINYINT(1) NOT NULL DEFAULT 0,
    size_bytes        INT NOT NULL DEFAULT 0,
    is_read           TINYINT(1) NOT NULL DEFAULT 0,
    is_flagged        TINYINT(1) NOT NULL DEFAULT 0,
    is_draft          TINYINT(1) NOT NULL DEFAULT 0,
    is_answered       TINYINT(1) NOT NULL DEFAULT 0,
    is_forwarded      TINYINT(1) NOT NULL DEFAULT 0,
    importance        VARCHAR(6) NOT NULL DEFAULT 'normal',
    send_status       VARCHAR(10) NULL,
    send_attempts     INT NOT NULL DEFAULT 0,
    send_error        TEXT NULL,
    scheduled_at      DATETIME NULL,
    sent_at           DATETIME NULL,
    received_at       DATETIME NULL,
    internal_date     DATETIME NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted_at        DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_acct_folder_uid (account_id, folder_id, imap_uid),
    KEY idx_mail_msg_folder_date (folder_id, internal_date),
    KEY idx_mail_msg_user_unread (user_id, is_read),
    KEY idx_mail_msg_conv (account_id, conversation_id),
    KEY idx_mail_msg_msgid (account_id, message_id_hdr),
    KEY idx_mail_msg_send (send_status, scheduled_at),
    CONSTRAINT fk_mail_msg_acct FOREIGN KEY (account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE,
    CONSTRAINT fk_mail_msg_folder FOREIGN KEY (folder_id) REFERENCES mail_folders(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 4. mail_attachments — metadata; bytes live in S3.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_attachments (
    id            BIGINT NOT NULL AUTO_INCREMENT,
    message_id    BIGINT NOT NULL,
    account_id    INT NOT NULL,
    user_id       INT NOT NULL,
    s3_key        VARCHAR(512) NULL,
    content_id    VARCHAR(255) NULL,
    file_name     VARCHAR(255) NOT NULL,
    mime_type     VARCHAR(120) NOT NULL,
    size_bytes    BIGINT NOT NULL DEFAULT 0,
    is_inline     TINYINT(1) NOT NULL DEFAULT 0,
    fetched       TINYINT(1) NOT NULL DEFAULT 0,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_mail_att_msg (message_id),
    CONSTRAINT fk_mail_att_msg FOREIGN KEY (message_id) REFERENCES mail_messages(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 5. mail_settings — one row per account, Outlook-parity preferences.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_settings (
    id                       INT NOT NULL AUTO_INCREMENT,
    account_id               INT NOT NULL,
    user_id                  INT NOT NULL,
    organization_id          INT NULL,
    focused_inbox            TINYINT(1) NOT NULL DEFAULT 0,
    conversation_view        TINYINT(1) NOT NULL DEFAULT 1,
    reading_pane             VARCHAR(6)  NOT NULL DEFAULT 'right',
    preview_lines            TINYINT NOT NULL DEFAULT 2,
    mark_read_behavior       VARCHAR(20) NOT NULL DEFAULT 'on_selection',
    mark_read_delay_seconds  INT NOT NULL DEFAULT 0,
    external_images          VARCHAR(6)  NOT NULL DEFAULT 'block',
    compose_format           VARCHAR(4)  NOT NULL DEFAULT 'html',
    default_font             VARCHAR(60) NOT NULL DEFAULT 'Calibri',
    default_font_size        INT NOT NULL DEFAULT 11,
    undo_send_seconds        INT NOT NULL DEFAULT 10,
    request_read_receipt     TINYINT(1) NOT NULL DEFAULT 0,
    request_delivery_receipt TINYINT(1) NOT NULL DEFAULT 0,
    read_receipt_response    VARCHAR(6)  NOT NULL DEFAULT 'ask',
    auto_reply_enabled       TINYINT(1) NOT NULL DEFAULT 0,
    auto_reply_subject       VARCHAR(255) NULL,
    auto_reply_message_html  MEDIUMTEXT NULL,
    auto_reply_start         DATETIME NULL,
    auto_reply_end           DATETIME NULL,
    auto_reply_internal_only TINYINT(1) NOT NULL DEFAULT 0,
    forwarding_enabled       TINYINT(1) NOT NULL DEFAULT 0,
    forwarding_address       VARCHAR(320) NULL,
    forwarding_keep_copy     TINYINT(1) NOT NULL DEFAULT 1,
    empty_trash_on_exit      TINYINT(1) NOT NULL DEFAULT 0,
    prefs_json               JSON NULL,
    updated_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_mail_settings_acct (account_id),
    CONSTRAINT fk_mail_set_acct FOREIGN KEY (account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 6. mail_signatures
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_signatures (
    id INT NOT NULL AUTO_INCREMENT, account_id INT NOT NULL, user_id INT NOT NULL,
    name VARCHAR(120) NOT NULL, body_html MEDIUMTEXT NOT NULL,
    is_default_new TINYINT(1) NOT NULL DEFAULT 0,
    is_default_reply TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id), KEY idx_mail_sig_acct (account_id),
    CONSTRAINT fk_mail_sig_acct FOREIGN KEY (account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 7. mail_rules — inbox filters (conditions JSON -> actions JSON).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_rules (
    id INT NOT NULL AUTO_INCREMENT, account_id INT NOT NULL, user_id INT NOT NULL,
    name VARCHAR(150) NOT NULL, is_enabled TINYINT(1) NOT NULL DEFAULT 1,
    priority INT NOT NULL DEFAULT 0,
    conditions_json JSON NOT NULL,
    actions_json    JSON NOT NULL,
    stop_processing TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id), KEY idx_mail_rule_acct (account_id, priority),
    CONSTRAINT fk_mail_rule_acct FOREIGN KEY (account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 8. mail_sender_lists — blocked & safe senders (junk control).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_sender_lists (
    id INT NOT NULL AUTO_INCREMENT, account_id INT NOT NULL, user_id INT NOT NULL,
    list_type VARCHAR(8) NOT NULL,
    address_or_domain VARCHAR(320) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_sender (account_id, list_type, address_or_domain),
    CONSTRAINT fk_mail_snd_acct FOREIGN KEY (account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 9. mail_categories — colored labels (Outlook categories).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_categories (
    id INT NOT NULL AUTO_INCREMENT, account_id INT NOT NULL, user_id INT NOT NULL,
    name VARCHAR(80) NOT NULL, color VARCHAR(20) NOT NULL DEFAULT 'blue',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id), UNIQUE KEY uq_cat (account_id, name),
    CONSTRAINT fk_mail_cat_acct FOREIGN KEY (account_id) REFERENCES mail_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 10. mail_message_categories — M:N join.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_message_categories (
    message_id BIGINT NOT NULL, category_id INT NOT NULL,
    PRIMARY KEY (message_id, category_id),
    CONSTRAINT fk_mmc_msg FOREIGN KEY (message_id) REFERENCES mail_messages(id) ON DELETE CASCADE,
    CONSTRAINT fk_mmc_cat FOREIGN KEY (category_id) REFERENCES mail_categories(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
