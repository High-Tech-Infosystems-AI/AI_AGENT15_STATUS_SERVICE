-- v28_clawback_lists.sql
-- Clawback WhatsApp notifications v1.3 (CLAWBACK_WHATSAPP_API_CONTRACT.md §6.4):
--   * Recruiters get one list a day even for a single candidate:
--     recruiter_batch_threshold default 1 -> 0 (list when due > threshold).
--   * Team summaries: senior managers daily; new "Admins – daily" rule
--     (audience `admins`, whole organisation).
--   * Each rule says what the recruiter should do:
--     clawback_notification_rules.action_text VARCHAR(120) NULL, filled for
--     the seeded rules by name.
--
-- Idempotent (MySQL 8.4): the column is added only if missing
-- (information_schema + prepared statement, as in v27). Safe to re-run.
-- Run on ats_staging first, then ats_basic (prod).

-- 1. action_text VARCHAR(120) NULL, if missing.
SET @has_col := (
  SELECT COUNT(*) FROM information_schema.columns
   WHERE table_schema = DATABASE()
     AND table_name   = 'clawback_notification_rules'
     AND column_name  = 'action_text'
);
SET @sql := IF(@has_col = 0,
  'ALTER TABLE clawback_notification_rules ADD COLUMN action_text VARCHAR(120) NULL AFTER stage_label',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2. Defaults for the seeded rules that have none yet.
UPDATE clawback_notification_rules
   SET action_text = CASE name
         WHEN 'Offer accepted, pre-joining' THEN 'Call to confirm joining'
         WHEN 'DOJ to Day 7'                THEN 'First-week call'
         WHEN 'Days 8–30'                   THEN 'Weekly check-in call'
         WHEN 'Days 31–60'                  THEN 'Weekly check-in call'
         WHEN 'Days 61–75'                  THEN 'Twice-weekly check-in'
         WHEN 'Days 76–90'                  THEN 'Close follow-up call'
         WHEN 'Cooling completed'           THEN 'Final status validation'
       END
 WHERE action_text IS NULL
   AND name IN ('Offer accepted, pre-joining', 'DOJ to Day 7', 'Days 8–30', 'Days 31–60',
                'Days 61–75', 'Days 76–90', 'Cooling completed');

-- 3. New default for recruiter_batch_threshold (rows created from now on).
ALTER TABLE clawback_notification_settings
  ALTER COLUMN recruiter_batch_threshold SET DEFAULT 0;

-- 4. Existing rows that still hold the previous default 1 move to 0.
UPDATE clawback_notification_settings
   SET recruiter_batch_threshold = 0
 WHERE recruiter_batch_threshold = 1;

-- 5. New digest audience `admins` and scope `organisation` (both ENUMs in v26).
--    MODIFY with the full value list is a no-op when re-run.
ALTER TABLE clawback_digest_rules
  MODIFY audience_type ENUM('team_managers','has_subordinate_managers','designation_levels',
                            'roles','users','admins') NOT NULL,
  MODIFY scope ENUM('direct_team','full_subtree','organisation') NOT NULL DEFAULT 'full_subtree';

-- 6. Senior managers get today's list every day (was weekly on Mondays).
UPDATE clawback_digest_rules
   SET name = 'Senior managers – daily', frequency_type = 'daily', frequency_value = JSON_OBJECT()
 WHERE name = 'Senior managers – weekly' AND frequency_type = 'weekly' AND deleted_at IS NULL;

-- 7. Admins and super admins (by role type) get the whole organisation's list
--    daily: one rule per org that has the seeded team-lead rule and no
--    admins rule yet. Priority 30 beats team leads (10) / senior managers (20).
INSERT INTO clawback_digest_rules
  (organization_id, name, audience_type, audience_value, scope, frequency_type,
   frequency_value, send_time, template_id, sections, skip_if_empty, priority, is_active,
   created_by, updated_by)
SELECT t.organization_id, 'Admins – daily', 'admins', JSON_OBJECT(), 'organisation', 'daily',
       JSON_OBJECT(), '09:30', t.template_id, t.sections, 1, 30, 1, t.created_by, t.created_by
  FROM clawback_digest_rules t
 WHERE t.name = 'Team leads – daily' AND t.deleted_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM (SELECT organization_id FROM clawback_digest_rules
                                   WHERE audience_type = 'admins' AND deleted_at IS NULL) a
                    WHERE a.organization_id = t.organization_id);
