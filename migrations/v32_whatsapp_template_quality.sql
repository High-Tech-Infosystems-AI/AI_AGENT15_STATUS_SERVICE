-- v32_whatsapp_template_quality.sql
-- Adds to whatsapp_templates:
--   quality_score  VARCHAR(16) NULL   GREEN | YELLOW | RED | UNKNOWN, from Meta's
--                                     message_template_quality_update webhook
--
-- This service does not run migrations: run this file by hand BEFORE deploying
-- the code that reads this column (the ORM selects it on every template query).
-- Idempotent (MySQL 8.x), same pattern as v29-v31. Run on ats_staging first,
-- then ats_basic (prod).

SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates'
    AND column_name = 'quality_score');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN quality_score VARCHAR(16) NULL AFTER previous_category',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
