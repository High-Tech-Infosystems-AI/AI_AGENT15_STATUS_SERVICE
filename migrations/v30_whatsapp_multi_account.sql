-- v30_whatsapp_multi_account.sql
-- Multi-account WhatsApp (channels contract §2.1, §2.3):
--   * whatsapp_provider_accounts: drop the one-account-per-org-per-provider
--     unique key, add deleted_at (soft delete), add idx_wa_acct_org.
--   * chn_channel_defaults: created here too (same DDL as the channel
--     service) so the status service's account resolution can read it even
--     if this runs first. The channel service is its only writer.
--
-- The channel service applies the same changes at startup; this file is for
-- DBAs. Idempotent (MySQL 8.x), same pattern as v27/v29: every step checks
-- information_schema first. Run on ats_staging first, then ats_basic (prod).

-- 1. Drop uq_wa_acct_org_provider if present.
SET @has_idx := (
  SELECT COUNT(*) FROM information_schema.statistics
   WHERE table_schema = DATABASE()
     AND table_name   = 'whatsapp_provider_accounts'
     AND index_name   = 'uq_wa_acct_org_provider'
);
SET @sql := IF(@has_idx > 0,
  'ALTER TABLE whatsapp_provider_accounts DROP INDEX uq_wa_acct_org_provider',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2. Add deleted_at if missing.
SET @has_col := (
  SELECT COUNT(*) FROM information_schema.columns
   WHERE table_schema = DATABASE()
     AND table_name   = 'whatsapp_provider_accounts'
     AND column_name  = 'deleted_at'
);
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_provider_accounts ADD COLUMN deleted_at DATETIME NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3. Add idx_wa_acct_org (organization_id, status) if missing.
SET @has_idx := (
  SELECT COUNT(*) FROM information_schema.statistics
   WHERE table_schema = DATABASE()
     AND table_name   = 'whatsapp_provider_accounts'
     AND index_name   = 'idx_wa_acct_org'
);
SET @sql := IF(@has_idx = 0,
  'ALTER TABLE whatsapp_provider_accounts ADD KEY idx_wa_acct_org (organization_id, status)',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 4. Channel defaults (contract §2.3).
CREATE TABLE IF NOT EXISTS chn_channel_defaults (
  id               INT NOT NULL AUTO_INCREMENT,
  organization_id  INT NOT NULL,
  purpose          VARCHAR(40) NOT NULL,
  channel_type     ENUM('whatsapp','email') NOT NULL,
  account_id       INT NOT NULL,
  updated_by       INT NULL,
  updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chn_default (organization_id, purpose, channel_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
