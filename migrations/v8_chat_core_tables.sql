-- v8_chat_core_tables.sql
-- Creates the foundational chat tables: conversations, members, messages, attachments.

CREATE TABLE IF NOT EXISTS chat_message_attachments (
    id              INT NOT NULL AUTO_INCREMENT,
    s3_key          VARCHAR(512) NOT NULL,
    mime_type       VARCHAR(100) NOT NULL,
    file_name       VARCHAR(255) NOT NULL,
    size_bytes      BIGINT NOT NULL,
    duration_seconds INT NULL,
    waveform_json   TEXT NULL,
    thumbnail_s3_key VARCHAR(512) NULL,
    uploaded_by     INT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_uploaded_by (uploaded_by),
    CONSTRAINT fk_chat_attach_user FOREIGN KEY (uploaded_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_conversations (
    id              INT NOT NULL AUTO_INCREMENT,
    type            VARCHAR(10) NOT NULL,
    team_id         INT NULL,
    title           VARCHAR(255) NULL,
    created_by      INT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_message_at DATETIME NULL,
    deleted_at      DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_team_conv (team_id),
    KEY idx_type (type),
    KEY idx_last_message_at (last_message_at),
    CONSTRAINT fk_chat_conv_team FOREIGN KEY (team_id) REFERENCES teams(id),
    CONSTRAINT fk_chat_conv_user FOREIGN KEY (created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_conversation_members (
    id              INT NOT NULL AUTO_INCREMENT,
    conversation_id INT NOT NULL,
    user_id         INT NOT NULL,
    role_in_conversation VARCHAR(20) NOT NULL DEFAULT 'member',
    joined_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_read_message_id BIGINT NULL,
    last_read_at    DATETIME NULL,
    muted           TINYINT(1) NOT NULL DEFAULT 0,
    archived        TINYINT(1) NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_user (conversation_id, user_id),
    KEY idx_user_conv (user_id, conversation_id),
    CONSTRAINT fk_chat_member_conv FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_member_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_messages (
    id              BIGINT NOT NULL AUTO_INCREMENT,
    conversation_id INT NOT NULL,
    sender_id       INT NOT NULL,
    message_type    VARCHAR(10) NOT NULL DEFAULT 'text',
    body            TEXT NULL,
    attachment_id   INT NULL,
    reply_to_message_id BIGINT NULL,
    forwarded_from_message_id BIGINT NULL,
    edited_at       DATETIME NULL,
    deleted_at      DATETIME NULL,
    deleted_by      INT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_conv_created (conversation_id, created_at),
    KEY idx_sender (sender_id),
    KEY idx_reply_to (reply_to_message_id),
    KEY idx_forwarded_from (forwarded_from_message_id),
    KEY idx_attachment (attachment_id),
    CONSTRAINT fk_chat_msg_conv FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id),
    CONSTRAINT fk_chat_msg_sender FOREIGN KEY (sender_id) REFERENCES users(id),
    CONSTRAINT fk_chat_msg_attachment FOREIGN KEY (attachment_id) REFERENCES chat_message_attachments(id),
    CONSTRAINT fk_chat_msg_reply FOREIGN KEY (reply_to_message_id) REFERENCES chat_messages(id),
    CONSTRAINT fk_chat_msg_fwd FOREIGN KEY (forwarded_from_message_id) REFERENCES chat_messages(id),
    CONSTRAINT fk_chat_msg_deleted_by FOREIGN KEY (deleted_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
