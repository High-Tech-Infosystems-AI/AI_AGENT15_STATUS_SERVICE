-- v29_whatsapp_parameter_format.sql
-- whatsapp_templates.parameter_format VARCHAR(16) NULL: NAMED templates need
-- each variable sent as {"parameter_name", "text"} (contract §6.6). Template
-- sync fills it from the provider's parameterFormat.
--
-- Idempotent (MySQL 8.4), as in v27. Run before deploying the code that reads
-- it. Run on ats_staging first, then ats_basic (prod).
SET @has_col := (
  SELECT COUNT(*) FROM information_schema.columns
   WHERE table_schema = DATABASE()
     AND table_name   = 'whatsapp_templates'
     AND column_name  = 'parameter_format'
);
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN parameter_format VARCHAR(16) NULL AFTER variable_map',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
