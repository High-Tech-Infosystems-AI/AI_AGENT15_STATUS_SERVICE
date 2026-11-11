-- v31_whatsapp_template_editor.sql
-- WhatsApp template editor (docs/TEMPLATES_CONTRACT.md), mirroring BroadNet's
-- template product. Adds to whatsapp_templates:
--   template_type            VARCHAR(32) NULL   DEFAULT | CATALOGUE | CALLING_PERMISSION_REQUEST | OTP
--   variable_type            VARCHAR(8)  NULL   NUMBER | NAME (NULL = worked out from the body)
--   header_media             JSON NULL          uploaded file / URL for IMAGE, VIDEO, DOCUMENT headers
--   header_sample_values     JSON NULL          samples for header variables
--   limited_time_offer       JSON NULL          {text, expires_at, has_expiration}
--   carousel_cards           JSON NULL          [{media, body_text, sample_values, buttons}]
--   auth_config              JSON NULL          {code_delivery, apps[], add_security_recommendation, ...}
--   custom_validity_period   TINYINT NOT NULL DEFAULT 0
--   validity_period_seconds  INT NULL
-- and index idx_wa_tpl_org_created (organization_id, created_at) for the
-- list's date filter. v26 created `category` as ENUM('UTILITY','MARKETING')
-- and `header_format` as ENUM('NONE','TEXT'); both become VARCHAR (as the
-- ORM model already declares) so AUTHENTICATION and the IMAGE / VIDEO /
-- DOCUMENT / LOCATION / CAROUSEL headers can be stored. Existing values stay.
--
-- This service does not run migrations: run this file by hand before
-- deploying the code that reads these columns. Idempotent (MySQL 8.x), same
-- pattern as v29/v30: every step checks information_schema first. Run on
-- ats_staging first, then ats_basic (prod).

-- 0a. category ENUM -> VARCHAR(20) (adds AUTHENTICATION)
SET @is_enum := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates'
    AND column_name = 'category' AND data_type = 'enum');
SET @sql := IF(@is_enum > 0,
  'ALTER TABLE whatsapp_templates MODIFY COLUMN category VARCHAR(20) NOT NULL DEFAULT ''UTILITY''',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 0b. header_format ENUM -> VARCHAR(10) (adds IMAGE, VIDEO, DOCUMENT, LOCATION, CAROUSEL)
SET @is_enum := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates'
    AND column_name = 'header_format' AND data_type = 'enum');
SET @sql := IF(@is_enum > 0,
  'ALTER TABLE whatsapp_templates MODIFY COLUMN header_format VARCHAR(10) NOT NULL DEFAULT ''NONE''',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 1. template_type
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'template_type');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN template_type VARCHAR(32) NULL AFTER category',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2. variable_type
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'variable_type');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN variable_type VARCHAR(8) NULL AFTER purpose',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3. header_media
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'header_media');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN header_media JSON NULL AFTER header_text',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 4. header_sample_values
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'header_sample_values');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN header_sample_values JSON NULL AFTER header_media',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 5. limited_time_offer
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'limited_time_offer');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN limited_time_offer JSON NULL AFTER buttons',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 6. carousel_cards
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'carousel_cards');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN carousel_cards JSON NULL AFTER limited_time_offer',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 7. auth_config
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'auth_config');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN auth_config JSON NULL AFTER carousel_cards',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 8. custom_validity_period
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'custom_validity_period');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN custom_validity_period TINYINT NOT NULL DEFAULT 0 AFTER auth_config',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 9. validity_period_seconds
SET @has_col := (SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND column_name = 'validity_period_seconds');
SET @sql := IF(@has_col = 0,
  'ALTER TABLE whatsapp_templates ADD COLUMN validity_period_seconds INT NULL AFTER custom_validity_period',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 10. idx_wa_tpl_org_created (organization_id, created_at)
SET @has_idx := (SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'whatsapp_templates' AND index_name = 'idx_wa_tpl_org_created');
SET @sql := IF(@has_idx = 0,
  'ALTER TABLE whatsapp_templates ADD KEY idx_wa_tpl_org_created (organization_id, created_at)',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 11. Backfill: existing rows were Default templates (Authentication rows: OTP).
UPDATE whatsapp_templates
   SET template_type = IF(category = 'AUTHENTICATION', 'OTP', 'DEFAULT')
 WHERE template_type IS NULL;
