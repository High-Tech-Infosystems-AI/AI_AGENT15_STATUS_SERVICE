-- v30_outgoing_webhooks.sql
-- Outgoing webhooks (app/webhooks_layer): endpoints an organisation registers
-- and the log of every event delivered to them.
--
-- Idempotent. Run before deploying the code that uses it. Run on ats_staging
-- first, then ats_basic (prod).
CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id                   INT AUTO_INCREMENT PRIMARY KEY,
  organization_id      INT NOT NULL,
  name                 VARCHAR(150) NOT NULL,
  description          VARCHAR(500) NULL,
  url                  VARCHAR(2048) NOT NULL,
  secret_enc           VARBINARY(512) NOT NULL,
  secret_last4         VARCHAR(4) NOT NULL,
  events               JSON NOT NULL,
  is_active            TINYINT NOT NULL DEFAULT 1,
  disabled_reason      VARCHAR(255) NULL,
  consecutive_failures INT NOT NULL DEFAULT 0,
  last_delivery_at     DATETIME NULL,
  last_success_at      DATETIME NULL,
  created_by           INT NULL,
  created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           DATETIME NULL ON UPDATE CURRENT_TIMESTAMP,
  deleted_at           DATETIME NULL,
  INDEX ix_webhook_endpoints_org (organization_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  endpoint_id     INT NOT NULL,
  organization_id INT NOT NULL,
  event_id        VARCHAR(64) NOT NULL,
  event_name      VARCHAR(100) NOT NULL,
  payload         MEDIUMTEXT NOT NULL,
  status          VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts        INT NOT NULL DEFAULT 0,
  max_attempts    INT NOT NULL DEFAULT 6,
  next_attempt_at DATETIME NULL,
  claimed_at      DATETIME NULL,
  response_status INT NULL,
  response_body   TEXT NULL,
  error           VARCHAR(500) NULL,
  duration_ms     INT NULL,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  delivered_at    DATETIME NULL,
  INDEX ix_webhook_deliveries_endpoint (endpoint_id, created_at),
  INDEX ix_webhook_deliveries_due (status, next_attempt_at),
  CONSTRAINT fk_webhook_deliveries_endpoint FOREIGN KEY (endpoint_id)
    REFERENCES webhook_endpoints (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
