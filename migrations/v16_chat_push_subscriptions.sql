-- v16_chat_push_subscriptions.sql
-- Web Push (VAPID) subscription registry. One row per (user, browser/device).
-- Endpoint is the canonical subscription identifier; uniqueness on it lets us
-- upsert without dedicated client IDs.

CREATE TABLE IF NOT EXISTS chat_push_subscriptions (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    endpoint      VARCHAR(2048) NOT NULL,
    endpoint_hash CHAR(64) NOT NULL,           -- sha256 of endpoint, used as unique key
    p256dh        VARCHAR(255) NOT NULL,
    auth_secret   VARCHAR(255) NOT NULL,
    user_agent    VARCHAR(512) NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at  TIMESTAMP NULL,

    CONSTRAINT uq_chat_push_endpoint UNIQUE (endpoint_hash),
    INDEX idx_chat_push_user (user_id),
    CONSTRAINT fk_chat_push_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
