-- v9_chat_message_metadata.sql
-- Mentions, edits, reads, deliveries.

CREATE TABLE IF NOT EXISTS chat_message_mentions (
    id                  INT NOT NULL AUTO_INCREMENT,
    message_id          BIGINT NOT NULL,
    mentioned_user_id   INT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_msg_user (message_id, mentioned_user_id),
    KEY idx_user (mentioned_user_id),
    CONSTRAINT fk_mention_msg FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE,
    CONSTRAINT fk_mention_user FOREIGN KEY (mentioned_user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_message_edits (
    id            INT NOT NULL AUTO_INCREMENT,
    message_id    BIGINT NOT NULL,
    previous_body TEXT NOT NULL,
    edited_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_msg (message_id),
    CONSTRAINT fk_edit_msg FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_message_reads (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    message_id   BIGINT NOT NULL,
    user_id      INT NOT NULL,
    read_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_msg_user (message_id, user_id),
    KEY idx_user (user_id),
    CONSTRAINT fk_read_msg FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE,
    CONSTRAINT fk_read_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_message_deliveries (
    id            BIGINT NOT NULL AUTO_INCREMENT,
    message_id    BIGINT NOT NULL,
    user_id       INT NOT NULL,
    delivered_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_msg_user (message_id, user_id),
    CONSTRAINT fk_delivery_msg FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE,
    CONSTRAINT fk_delivery_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
