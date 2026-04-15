-- ============================================================================
-- Notification Service — V2 Migration
-- Adds:
--   1. 'log' as a third delivery_mode (alongside push / banner)
--   2. Dual-delivery columns on notification_events (also_banner + banner_*)
--   3. Two new events: job_deadline_updated, job_positions_updated
--   4. Re-classification of existing events into push / banner+push / log
-- Idempotent — safe to re-run (uses INFORMATION_SCHEMA checks for columns,
-- ON DUPLICATE KEY for inserts).
-- ============================================================================

-- ---- 1. Add dual-delivery columns to notification_events (idempotent) ----
DROP PROCEDURE IF EXISTS _notif_add_col;
DELIMITER $$
CREATE PROCEDURE _notif_add_col(
    IN col_name VARCHAR(64),
    IN col_def  VARCHAR(1000)
)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'notification_events'
          AND COLUMN_NAME  = col_name
    ) THEN
        SET @sql = CONCAT('ALTER TABLE notification_events ADD COLUMN ', col_name, ' ', col_def);
        PREPARE stmt FROM @sql;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

CALL _notif_add_col('also_banner',
    'TINYINT(1) NOT NULL DEFAULT 0 COMMENT ''1 = also fire a banner alongside the primary delivery''');
CALL _notif_add_col('banner_title_template',
    'VARCHAR(255) DEFAULT NULL');
CALL _notif_add_col('banner_message_template',
    'TEXT DEFAULT NULL');
CALL _notif_add_col('banner_target_type',
    'VARCHAR(20) DEFAULT NULL COMMENT ''Overrides target_type for the banner half (role|job|user|all)''');
CALL _notif_add_col('banner_target_roles',
    'VARCHAR(255) DEFAULT NULL');
CALL _notif_add_col('banner_expires_hours',
    'INT DEFAULT NULL COMMENT ''Banner TTL in hours (event_handler defaults to 24 if NULL)''');

DROP PROCEDURE _notif_add_col;


-- ---- 2. Re-classify existing events ----

-- Everything becomes a log by default; individual events below get upgraded.
UPDATE notification_events SET delivery_mode = 'log';

-- -------------------- LOGIN SERVICE --------------------
UPDATE notification_events SET delivery_mode = 'push', priority = 'medium'
    WHERE event_name = 'password_changed';
UPDATE notification_events SET delivery_mode = 'push', priority = 'critical'
    WHERE event_name = 'illegal_login_attempt';
UPDATE notification_events SET delivery_mode = 'push', priority = 'high'
    WHERE event_name = 'wrong_password_threshold';

-- -------------------- JOB SERVICE --------------------
-- Push-only (notification to job assignees)
UPDATE notification_events SET delivery_mode = 'push'
    WHERE event_name IN (
        'job_updated',
        'spoc_assigned_to_stage',
        'spoc_assignment_updated',
        'spoc_assignment_removed',
        'recruiter_assigned_to_job',
        'recruiter_removed_from_job'
    );

-- Push + Banner: candidate_joined
--   Notification (target=user who did it): "You closed one position for <job>."
--   Banner (target=job assignees):         "1 position closed by <user> for <job>."
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'high',
    target_type = 'user',
    target_roles = NULL,
    default_title_template = 'Position Closed: {job_title}',
    default_message_template = 'You closed one position for "{job_title}" — {candidate_name} has joined. Great work!',
    also_banner = 1,
    banner_title_template = 'Position Closed: {job_title}',
    banner_message_template = '1 position closed by {user_name} for job "{job_title}" — {candidate_name} has joined. Congratulations to the team!',
    banner_target_type = 'job',
    banner_target_roles = NULL,
    banner_expires_hours = 48
WHERE event_name = 'candidate_joined';

-- Push + Banner: candidate_rejected — both go to admins/super_admins only
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'role',
    target_roles = 'super_admin,admin',
    default_title_template = 'Candidate Rejected: {candidate_name}',
    default_message_template = '{candidate_name} has been rejected for job "{job_title}". Reason: {reason}',
    also_banner = 1,
    banner_title_template = 'Rejection Recorded: {candidate_name}',
    banner_message_template = 'Candidate {candidate_name} was rejected for job "{job_title}" by {user_name}. Reason: {reason}',
    banner_target_type = 'role',
    banner_target_roles = 'super_admin,admin',
    banner_expires_hours = 24
WHERE event_name = 'candidate_rejected';

-- Push + Banner: clawback_marked_complete (same pattern as joined)
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'user',
    target_roles = NULL,
    default_title_template = 'Clawback Complete: {candidate_name}',
    default_message_template = 'You marked clawback complete for {candidate_name} on job "{job_title}".',
    also_banner = 1,
    banner_title_template = 'Clawback Complete: {candidate_name}',
    banner_message_template = 'Clawback period for {candidate_name} on job "{job_title}" is complete (marked by {user_name}).',
    banner_target_type = 'job',
    banner_target_roles = NULL,
    banner_expires_hours = 24
WHERE event_name = 'clawback_marked_complete';

-- Push + Banner: deadline approaching / exceeded
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'high',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 1,
    banner_title_template = 'Deadline Approaching: {job_title}',
    banner_message_template = 'Job "{job_title}" deadline is in {days_remaining} day(s) — {deadline}.',
    banner_target_type = 'job',
    banner_target_roles = NULL,
    banner_expires_hours = 24
WHERE event_name = 'job_deadline_approaching';

UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'critical',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 1,
    banner_title_template = 'Deadline Exceeded: {job_title}',
    banner_message_template = 'Job "{job_title}" deadline ({deadline}) has been exceeded!',
    banner_target_type = 'job',
    banner_target_roles = NULL,
    banner_expires_hours = 72
WHERE event_name = 'job_deadline_exceeded';

-- -------------------- AI / RESUME ANALYZER --------------------
-- Completions become push so the user knows work is done and ready to review.
UPDATE notification_events SET delivery_mode = 'push', priority = 'medium'
    WHERE event_name IN (
        'resume_parse_complete',
        'resume_match_complete',
        'resume_upgrade_complete',
        'resume_summarize_complete'
    );

-- Only match + summarize failures push (per product spec);
-- parse_failed and upgrade_failed stay as log (they're handled in the task UI progress stream).
UPDATE notification_events SET delivery_mode = 'push', priority = 'high'
    WHERE event_name IN (
        'resume_match_failed',
        'resume_summarize_failed',
        'scraping_activity_detected'
    );

-- -------------------- BULK CANDIDATE --------------------
UPDATE notification_events SET delivery_mode = 'push', priority = 'medium'
    WHERE event_name = 'bulk_processing_complete';

-- -------------------- CANDIDATE --------------------
-- Both individual and bulk assignment notify — so recruiters know they have new work.
UPDATE notification_events SET delivery_mode = 'push', priority = 'medium'
    WHERE event_name IN ('candidate_assigned_to_user', 'candidate_bulk_assigned');

-- -------------------- RBAC --------------------
UPDATE notification_events SET delivery_mode = 'push', priority = 'high'
    WHERE event_name IN ('user_account_disabled', 'user_role_changed');


-- ---- 3. Add new events for banner-worthy job updates ----
-- These are fired by the job service when the specific field changed.
-- The job service should call this event INSTEAD OF job_updated when a deadline
-- or no-of-positions change happens, so the frontend gets a banner too.
INSERT INTO notification_events
    (event_name, description, default_title_template, default_message_template,
     domain_type, visibility, target_type, target_roles, source_service,
     priority, delivery_mode,
     also_banner, banner_title_template, banner_message_template,
     banner_target_type, banner_target_roles, banner_expires_hours)
VALUES
    ('job_deadline_updated',
     'Job deadline was changed — banner + notification to job assignees',
     'Deadline Updated: {job_title}',
     'Deadline for job "{job_title}" changed from {old_deadline} to {new_deadline}.',
     'jobs', 'restricted', 'job', NULL, 'job',
     'high', 'push',
     1,
     'Deadline Updated: {job_title}',
     'New deadline for "{job_title}": {new_deadline} (was {old_deadline}). Updated by {user_name}.',
     'job', NULL, 48),

    ('job_positions_updated',
     'Number of open positions changed — banner + notification to job assignees',
     'Positions Updated: {job_title}',
     'Open positions for job "{job_title}" changed from {old_positions} to {new_positions}.',
     'jobs', 'restricted', 'job', NULL, 'job',
     'high', 'push',
     1,
     'Positions Updated: {job_title}',
     '"{job_title}" now has {new_positions} open positions (was {old_positions}). Updated by {user_name}.',
     'job', NULL, 48)
ON DUPLICATE KEY UPDATE
    default_title_template = VALUES(default_title_template),
    default_message_template = VALUES(default_message_template),
    delivery_mode = VALUES(delivery_mode),
    also_banner = VALUES(also_banner),
    banner_title_template = VALUES(banner_title_template),
    banner_message_template = VALUES(banner_message_template),
    banner_target_type = VALUES(banner_target_type),
    banner_expires_hours = VALUES(banner_expires_hours);
