-- v14_chat_reactions.sql
-- Per-user emoji reactions on a chat message. Unique on (message, user, emoji)
-- so the same person can react with multiple distinct emojis but not the same
-- emoji twice. ORDER BY created_at gives reaction-feed-style stability.

CREATE TABLE IF NOT EXISTS chat_message_reactions (
    id           BIGINT NOT NULL AUTO_INCREMENT,
    message_id   BIGINT NOT NULL,
    user_id      INT NOT NULL,
    emoji        VARCHAR(32) NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_msg_user_emoji (message_id, user_id, emoji),
    KEY idx_message_id (message_id),
    KEY idx_user (user_id),
    CONSTRAINT fk_react_msg
        FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE,
    CONSTRAINT fk_react_user
        FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
