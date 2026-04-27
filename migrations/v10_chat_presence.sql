-- v10_chat_presence.sql
CREATE TABLE IF NOT EXISTS chat_user_presence (
    user_id      INT NOT NULL,
    status       VARCHAR(10) NOT NULL DEFAULT 'offline',
    last_seen_at DATETIME NULL,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    CONSTRAINT fk_presence_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
