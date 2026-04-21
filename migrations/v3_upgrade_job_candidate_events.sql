-- ============================================================================
-- V3 Migration — Upgrade Job/Candidate events from 'log' to push/banner
--
-- The v2 migration defaulted everything to 'log' and upgraded a few events
-- individually. Many important Job/Candidate events stayed as 'log' which
-- meant no push notification + no banner. This migration upgrades them.
-- Idempotent — safe to run multiple times.
-- ============================================================================


-- -------------------- JOB SERVICE --------------------

-- job_created — banner for admins, push for admins, so they know about new jobs.
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'role',
    target_roles = 'super_admin,admin',
    also_banner = 1,
    banner_title_template = 'New Job: {job_title}',
    banner_message_template = 'A new job "{job_title}" (ID {job_public_id}) was created.',
    banner_target_type = 'role',
    banner_target_roles = 'super_admin,admin',
    banner_expires_hours = 24
WHERE event_name = 'job_created';

-- candidate_assigned_to_job — push to all job assignees
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'candidate_assigned_to_job';

-- candidate_pipeline_stage_changed — push to all job assignees
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'candidate_pipeline_stage_changed';

-- candidate_removed_from_job — push to job assignees
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'candidate_removed_from_job';

-- pipeline_stage_activity_added — push to job assignees
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'low',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'pipeline_stage_activity_added';

-- candidate_joined_updated — push + banner to job team
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 1,
    banner_title_template = 'Joining Updated: {candidate_name}',
    banner_message_template = 'Joining details for {candidate_name} (job "{job_title}") were updated.',
    banner_target_type = 'job',
    banner_target_roles = NULL,
    banner_expires_hours = 24
WHERE event_name = 'candidate_joined_updated';

-- candidate_rejected_updated — push to admin
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'role',
    target_roles = 'super_admin,admin',
    also_banner = 0
WHERE event_name = 'candidate_rejected_updated';

-- remarks — push (log-level info) to job assignees
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'low',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 0
WHERE event_name IN ('candidate_joined_remark_added', 'candidate_rejected_remark_added');

-- intimation_mail_sent — push + banner (big stakeholder-wide announcement)
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 1,
    banner_title_template = 'Intimation Sent: {job_title}',
    banner_message_template = 'Intimation email was sent to all stakeholders of job "{job_title}".',
    banner_target_type = 'job',
    banner_target_roles = NULL,
    banner_expires_hours = 24
WHERE event_name = 'intimation_mail_sent';


-- -------------------- CANDIDATE SERVICE --------------------

-- candidate_created — push to creator + admins
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'low',
    target_type = 'user',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'candidate_created';

-- candidate_updated — push to assigned user + admins
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'low',
    target_type = 'user',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'candidate_updated';

-- candidate_status_changed — push to assigned user + admins
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'user',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'candidate_status_changed';

-- candidate_remark_added — push to assigned user + admins
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'low',
    target_type = 'user',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'candidate_remark_added';

-- candidate_bulk_assigned_to_job — push to job assignees
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'job',
    target_roles = NULL,
    also_banner = 0
WHERE event_name = 'candidate_bulk_assigned_to_job';

-- candidate_bulk_assigned — push to admins
UPDATE notification_events SET
    delivery_mode = 'push',
    priority = 'medium',
    target_type = 'role',
    target_roles = 'super_admin,admin',
    also_banner = 0
WHERE event_name = 'candidate_bulk_assigned';


-- -------------------- Verify the results ----------------------
-- SELECT event_name, delivery_mode, also_banner, target_type, priority
-- FROM notification_events
-- WHERE source_service IN ('job', 'candidate')
-- ORDER BY delivery_mode, event_name;
