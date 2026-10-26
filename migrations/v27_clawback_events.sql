-- v27_clawback_events.sql
-- Clawback WhatsApp notifications v1.1 (CLAWBACK_WHATSAPP_API_CONTRACT.md §6):
--   * Events on/off: system:* toggles live in a new nullable JSON column
--     clawback_notification_settings.event_toggles
--     (e.g. {"recruiter_summary": false}); missing keys mean enabled.
--   * One message per recruiter per day: recruiter_batch_threshold default
--     changes from 3 to 1 (combine when due > threshold, i.e. 2+ due).
--
-- Idempotent (MySQL 8.4): the column is added only if missing
-- (information_schema + prepared statement, as in v21). Safe to re-run.
-- Run on ats_staging first, then ats_basic (prod).

-- 1. event_toggles JSON NULL, if missing.
SET @has_col := (
  SELECT COUNT(*) FROM information_schema.columns
   WHERE table_schema = DATABASE()
     AND table_name   = 'clawback_notification_settings'
     AND column_name  = 'event_toggles'
);
SET @sql := IF(@has_col = 0,
  'ALTER TABLE clawback_notification_settings ADD COLUMN event_toggles JSON NULL AFTER defaults_seeded_at',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2. New default for recruiter_batch_threshold (rows created from now on).
ALTER TABLE clawback_notification_settings
  ALTER COLUMN recruiter_batch_threshold SET DEFAULT 1;

-- 3. Existing rows that still hold the old default 3 move to 1.
UPDATE clawback_notification_settings
   SET recruiter_batch_threshold = 1
 WHERE recruiter_batch_threshold = 3;
