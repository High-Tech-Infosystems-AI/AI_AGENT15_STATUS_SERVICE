-- ============================================================================
-- Notification Service Tables — Migration Script
-- Run against the same MySQL database used by the Status Service (ats_staging)
--
-- For FRESH installs: run this file then v2_delivery_modes_and_banners.sql
--   to add log/dual-banner columns and re-classify seed events.
-- For EXISTING installs upgrading to v2: just run v2_delivery_modes_and_banners.sql.
-- ============================================================================

-- 1. Core notifications table
CREATE TABLE IF NOT EXISTS notifications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    delivery_mode VARCHAR(20) NOT NULL COMMENT 'push | banner | log',
    domain_type VARCHAR(30) NOT NULL COMMENT 'login | jobs | ai | candidate | security | system | user_management',
    visibility VARCHAR(20) NOT NULL COMMENT 'personal | public | restricted',
    priority VARCHAR(20) NOT NULL DEFAULT 'medium' COMMENT 'low | medium | high | critical',
    target_type VARCHAR(20) NOT NULL COMMENT 'all | user | job | role',
    target_id VARCHAR(255) DEFAULT NULL COMMENT 'csv user_ids | job id | role name',
    source_service VARCHAR(50) DEFAULT NULL COMMENT 'login | job | candidate | resume_analyzer | rbac | bulk_candidate | system',
    event_type VARCHAR(100) DEFAULT NULL COMMENT 'auto-notification event name',
    metadata TEXT DEFAULT NULL COMMENT 'JSON extra context',
    created_by INT DEFAULT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME DEFAULT NULL COMMENT 'banner expiration',
    is_active TINYINT(1) NOT NULL DEFAULT 1,

    INDEX idx_delivery_mode (delivery_mode),
    INDEX idx_domain_type (domain_type),
    INDEX idx_visibility (visibility),
    INDEX idx_priority (priority),
    INDEX idx_source_service (source_service),
    INDEX idx_event_type (event_type),
    INDEX idx_created_at (created_at),
    INDEX idx_is_active (is_active),
    INDEX idx_target_type (target_type),
    INDEX idx_filter_combo (domain_type, visibility, created_at, is_active),

    CONSTRAINT fk_notif_created_by FOREIGN KEY (created_by) REFERENCES users(id)
        ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- 2. Per-user recipient + read tracking
CREATE TABLE IF NOT EXISTS notification_recipients (
    id INT AUTO_INCREMENT PRIMARY KEY,
    notification_id INT NOT NULL,
    user_id INT NOT NULL,
    is_read TINYINT(1) NOT NULL DEFAULT 0,
    read_at DATETIME DEFAULT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uq_notif_user (notification_id, user_id),
    INDEX idx_user_read (user_id, is_read),
    INDEX idx_user_created (user_id, created_at),
    INDEX idx_notif_id (notification_id),

    CONSTRAINT fk_nr_notification FOREIGN KEY (notification_id) REFERENCES notifications(id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_nr_user FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- 3. Scheduled / recurring notifications
CREATE TABLE IF NOT EXISTS notification_schedules (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    delivery_mode VARCHAR(20) NOT NULL,
    domain_type VARCHAR(30) NOT NULL,
    visibility VARCHAR(20) NOT NULL,
    priority VARCHAR(20) NOT NULL DEFAULT 'medium',
    target_type VARCHAR(20) NOT NULL,
    target_id VARCHAR(255) DEFAULT NULL,
    metadata TEXT DEFAULT NULL,
    scheduled_at DATETIME NOT NULL,
    repeat_type VARCHAR(20) NOT NULL DEFAULT 'once' COMMENT 'once | daily | weekly',
    repeat_until DATETIME DEFAULT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT 'pending | sent | cancelled',
    last_sent_at DATETIME DEFAULT NULL,
    created_by INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_status_scheduled (status, scheduled_at),
    INDEX idx_schedule_created_by (created_by),

    CONSTRAINT fk_sched_created_by FOREIGN KEY (created_by) REFERENCES users(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- 4. Auto-notification event registry (seed data below)
CREATE TABLE IF NOT EXISTS notification_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    event_name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT DEFAULT NULL,
    default_title_template VARCHAR(255) NOT NULL,
    default_message_template TEXT NOT NULL,
    domain_type VARCHAR(30) NOT NULL,
    visibility VARCHAR(20) NOT NULL,
    target_type VARCHAR(20) NOT NULL COMMENT 'role | job | user | all',
    target_roles VARCHAR(255) DEFAULT NULL COMMENT 'csv of role names',
    source_service VARCHAR(50) NOT NULL,
    priority VARCHAR(20) NOT NULL DEFAULT 'medium',
    delivery_mode VARCHAR(20) NOT NULL DEFAULT 'push',
    is_enabled TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================================
-- SEED DATA — 65 auto-notification events
-- NOTE: Every notification always includes super_admin + admin as recipients
--       (enforced at the engine level in store.py).
--       Events with target_type='user' send to a specific user AND admins.
--       Events with target_type='role' send to the listed roles AND admins.
--       Events with target_type='job' send to job assignees AND admins.
-- ============================================================================

INSERT INTO notification_events
    (event_name, description, default_title_template, default_message_template, domain_type, visibility, target_type, target_roles, source_service, priority, delivery_mode)
VALUES

-- ---- LOGIN & AUTH (9 events) ----
-- NOTE: token_verification removed (not needed)

('user_signup_success',
 'New user registered on the platform — goes to the user + super_admin + admin',
 'Welcome to the platform!',
 'Your account has been created successfully. Welcome aboard, {username}!',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'low', 'push'),

('user_login_success',
 'User logged in — goes to the logged-in user + super_admin + admin',
 'Login Activity: {username}',
 'User {username} logged in from {ip_address} at {timestamp}.',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'low', 'push'),

('user_login_failed',
 'Failed login attempt (wrong credentials) — super_admin + admin',
 'Failed Login Attempt: {username}',
 'Failed login attempt for user {username} from IP {ip_address}. Reason: {reason}',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'medium', 'push'),

('user_logout',
 'User logged out — goes to the user + super_admin + admin',
 'Logout: {username}',
 'User {username} logged out at {timestamp}.',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'low', 'push'),

('password_changed',
 'User changed password, all sessions invalidated — user + super_admin + admin',
 'Password Changed: {username}',
 'User {username} changed their password. All active sessions have been invalidated.',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'medium', 'push'),

('forgot_password_otp_sent',
 'OTP sent for password reset — user + super_admin + admin',
 'Password Reset OTP Sent: {email}',
 'A password reset OTP has been sent to {email} for user {username}.',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'medium', 'push'),

('otp_validated',
 'OTP validated successfully — super_admin + admin',
 'OTP Validated: {email}',
 'OTP validated successfully for {email}.',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'low', 'push'),

('password_reset_complete',
 'Password reset via OTP flow completed — user + super_admin + admin',
 'Password Reset Complete: {email}',
 'Password has been reset successfully for {email}. All previous sessions invalidated.',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'medium', 'push'),

('role_created',
 'New role added to the system — super_admin + admin',
 'New Role Created: {role_name}',
 'A new role "{role_name}" has been created in the system.',
 'login', 'restricted', 'role', 'super_admin,admin', 'login', 'low', 'push'),


-- ---- JOBS & POSITIONS (22 events) ----
('job_created',
 'New job opening created',
 'New Job Created: {job_title}',
 'A new job opening "{job_title}" has been created.',
 'jobs', 'restricted', 'role', 'super_admin,admin', 'job', 'medium', 'push'),

('job_updated',
 'Job details modified',
 'Job Updated: {job_title}',
 'Job "{job_title}" has been updated. Changes: {changes}',
 'jobs', 'restricted', 'job', NULL, 'job', 'medium', 'push'),

('job_deadline_approaching',
 'Job deadline is within 3 days',
 'Deadline Approaching: {job_title}',
 'Job "{job_title}" deadline is approaching on {deadline}. {days_remaining} days remaining.',
 'jobs', 'restricted', 'job', NULL, 'system', 'high', 'push'),

('job_deadline_exceeded',
 'Job deadline has passed',
 'Deadline Exceeded: {job_title}',
 'Job "{job_title}" deadline ({deadline}) has been exceeded!',
 'jobs', 'restricted', 'job', NULL, 'system', 'critical', 'push'),

('candidate_assigned_to_job',
 'Candidate added to a job pipeline',
 'Candidate Assigned: {candidate_name}',
 'Candidate {candidate_name} has been assigned to job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),

('candidate_bulk_assigned_to_job',
 'Multiple candidates added to job',
 'Bulk Candidate Assignment: {job_title}',
 '{count} candidates have been bulk-assigned to job "{job_title}".',
 'jobs', 'restricted', 'job', NULL, 'job', 'medium', 'push'),

('candidate_pipeline_stage_changed',
 'Candidate moved through pipeline stage',
 'Pipeline Update: {candidate_name}',
 'Candidate {candidate_name} moved to stage "{stage_name}" for job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),

('candidate_joined',
 'Candidate accepted offer and joined',
 'Candidate Joined: {candidate_name}',
 '{candidate_name} has joined for the position "{job_title}". Congratulations!',
 'jobs', 'restricted', 'job', NULL, 'job', 'high', 'push'),

('candidate_joined_updated',
 'Joining details updated',
 'Joining Updated: {candidate_name}',
 'Joining details for {candidate_name} (job "{job_title}") have been updated.',
 'jobs', 'restricted', 'job', NULL, 'job', 'medium', 'push'),

('candidate_rejected',
 'Candidate rejected from job',
 'Candidate Rejected: {candidate_name}',
 '{candidate_name} has been rejected for job "{job_title}". Reason: {reason}',
 'jobs', 'restricted', 'job', NULL, 'job', 'medium', 'push'),

('candidate_rejected_updated',
 'Rejection details updated',
 'Rejection Updated: {candidate_name}',
 'Rejection details for {candidate_name} (job "{job_title}") have been updated.',
 'jobs', 'restricted', 'job', NULL, 'job', 'medium', 'push'),

('candidate_joined_remark_added',
 'Internal note on accepted candidate',
 'Remark on Joined: {candidate_name}',
 'A remark was added for joined candidate {candidate_name} on job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'low', 'push'),

('candidate_rejected_remark_added',
 'Internal note on rejected candidate',
 'Remark on Rejected: {candidate_name}',
 'A remark was added for rejected candidate {candidate_name} on job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'low', 'push'),

('clawback_marked_complete',
 'Clawback monitoring period finished',
 'Clawback Complete: {candidate_name}',
 'Clawback period for {candidate_name} on job "{job_title}" is now complete.',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),

('intimation_mail_sent',
 'Bulk intimation email sent to stakeholders',
 'Intimation Mail Sent: {job_title}',
 'Intimation email has been sent to all stakeholders for job "{job_title}".',
 'jobs', 'restricted', 'job', NULL, 'job', 'medium', 'push'),

('candidate_removed_from_job',
 'Candidate unassigned from job',
 'Candidate Removed: {candidate_name}',
 '{candidate_name} has been removed from job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),

('pipeline_stage_activity_added',
 'Activity log entry for pipeline stage',
 'Pipeline Activity: {candidate_name}',
 'New activity for {candidate_name} at stage "{stage_name}" on job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'low', 'push'),

('spoc_assigned_to_stage',
 'SPOC assigned to pipeline stage',
 'SPOC Assigned: {spoc_name}',
 '{spoc_name} has been assigned as SPOC for stage "{stage_name}" on job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),

('spoc_assignment_updated',
 'SPOC changed for pipeline stage',
 'SPOC Changed: {job_title}',
 'SPOC for stage "{stage_name}" on job "{job_title}" changed from {old_spoc} to {new_spoc}.',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),

('spoc_assignment_removed',
 'SPOC unassigned from pipeline stage',
 'SPOC Removed: {spoc_name}',
 '{spoc_name} has been removed as SPOC from stage "{stage_name}" on job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),

('recruiter_assigned_to_job',
 'Job assigned to recruiter(s)',
 'Job Assigned: {job_title}',
 'You have been assigned to job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),

('recruiter_removed_from_job',
 'Recruiter unassigned from job',
 'Job Unassigned: {job_title}',
 'You have been removed from job "{job_title}".',
 'jobs', 'restricted', 'user', NULL, 'job', 'medium', 'push'),


-- ---- AI & PROCESSING (14 events) ----
('resume_parse_started',
 'Resume parsing queued',
 'Resume Parsing Started',
 'Resume parsing has been queued for {candidate_name}.',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'low', 'push'),

('resume_parse_complete',
 'Resume parsed successfully',
 'Resume Parsed: {candidate_name}',
 'Resume for {candidate_name} has been parsed successfully.',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'medium', 'push'),

('resume_parse_failed',
 'Resume parsing failed',
 'Resume Parse Failed: {candidate_name}',
 'Resume parsing failed for {candidate_name}. Error: {error}',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'high', 'push'),

('resume_match_started',
 'Resume-JD matching queued',
 'Resume Matching Started',
 'Resume-JD matching has been queued for {candidate_name}.',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'low', 'push'),

('resume_match_complete',
 'Resume-JD matching done',
 'Resume Matched: {candidate_name}',
 'Resume-JD matching for {candidate_name} completed. Match score: {score}%.',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'medium', 'push'),

('resume_match_failed',
 'Resume-JD matching failed',
 'Resume Match Failed: {candidate_name}',
 'Resume-JD matching failed for {candidate_name}. Error: {error}',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'high', 'push'),

('resume_summarize_started',
 'Summarization queued',
 'Resume Summarization Started',
 'Resume summarization has been queued for {candidate_name}.',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'low', 'push'),

('resume_summarize_complete',
 'Summarization done',
 'Resume Summarized: {candidate_name}',
 'Resume summarization for {candidate_name} completed successfully.',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'medium', 'push'),

('resume_summarize_failed',
 'Summarization failed',
 'Resume Summarize Failed: {candidate_name}',
 'Resume summarization failed for {candidate_name}. Error: {error}',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'high', 'push'),

('resume_upgrade_started',
 'Resume upgrade queued',
 'Resume Upgrade Started',
 'Resume upgrade has been queued for {candidate_name}.',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'low', 'push'),

('resume_upgrade_complete',
 'Resume upgraded successfully',
 'Resume Upgraded: {candidate_name}',
 'Resume for {candidate_name} has been upgraded successfully.',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'medium', 'push'),

('resume_upgrade_failed',
 'Resume upgrade failed',
 'Resume Upgrade Failed: {candidate_name}',
 'Resume upgrade failed for {candidate_name}. Error: {error}',
 'ai', 'restricted', 'user', NULL, 'resume_analyzer', 'high', 'push'),

('bulk_processing_started',
 'Bulk file processing started',
 'Bulk Processing Started',
 'Bulk candidate file "{file_name}" processing has started. {row_count} rows detected.',
 'ai', 'restricted', 'user', NULL, 'bulk_candidate', 'medium', 'push'),

('bulk_processing_complete',
 'Bulk processing finished with stats',
 'Bulk Processing Complete',
 'Bulk processing of "{file_name}" completed. Created: {created}, Updated: {updated}, Failed: {failed}.',
 'ai', 'restricted', 'role', 'super_admin,admin', 'bulk_candidate', 'medium', 'push'),


-- ---- CANDIDATE (8 events) ----
('candidate_created',
 'New candidate profile created',
 'Candidate Created: {candidate_name}',
 'New candidate profile created for {candidate_name} ({candidate_email}).',
 'candidate', 'restricted', 'user', NULL, 'candidate', 'low', 'push'),

('candidate_created_bulk_upload',
 'Candidate imported from bulk file',
 'Bulk Import: {candidate_name}',
 'Candidate {candidate_name} was imported from bulk upload file "{file_name}".',
 'candidate', 'restricted', 'role', 'super_admin,admin', 'bulk_candidate', 'medium', 'push'),

('candidate_updated',
 'Candidate information modified',
 'Candidate Updated: {candidate_name}',
 'Candidate {candidate_name} profile has been updated. Fields changed: {fields}.',
 'candidate', 'restricted', 'user', NULL, 'candidate', 'low', 'push'),

('candidate_status_changed',
 'Candidate pipeline status updated',
 'Status Changed: {candidate_name}',
 'Candidate {candidate_name} status changed to "{new_status}".',
 'candidate', 'restricted', 'user', NULL, 'candidate', 'medium', 'push'),

('candidate_assigned_to_user',
 'Candidate assigned to recruiter',
 'Candidate Assigned to You: {candidate_name}',
 'Candidate {candidate_name} has been assigned to you.',
 'candidate', 'restricted', 'user', NULL, 'candidate', 'medium', 'push'),

('candidate_bulk_assigned',
 'Multiple candidates bulk-assigned',
 'Bulk Candidate Assignment',
 '{count} candidates have been assigned. Check your candidate list.',
 'candidate', 'restricted', 'role', 'super_admin,admin', 'candidate', 'medium', 'push'),

('candidate_remark_added',
 'Comment/remark added to candidate',
 'New Remark: {candidate_name}',
 'A new remark has been added for candidate {candidate_name}: "{remark_preview}".',
 'candidate', 'restricted', 'user', NULL, 'candidate', 'low', 'push'),

('candidate_duplicate_detected',
 'Duplicate candidate email detected',
 'Duplicate Detected: {candidate_email}',
 'A duplicate candidate was detected with email {candidate_email}.',
 'candidate', 'restricted', 'user', NULL, 'candidate', 'low', 'push'),


-- ---- SECURITY (3 events) ----
('illegal_login_attempt',
 'Suspicious/repeated failed login detected',
 'SECURITY ALERT: Illegal Login Attempt',
 'Suspicious login activity detected for user {username} from IP {ip_address}. {attempts} failed attempts.',
 'security', 'restricted', 'role', 'super_admin,admin', 'login', 'critical', 'push'),

('scraping_activity_detected',
 'Potential data scraping detected',
 'SECURITY ALERT: Scraping Activity',
 'Potential data scraping activity detected from IP {ip_address}. Action: {action}. Details: {details}',
 'security', 'restricted', 'role', 'super_admin,admin', 'resume_analyzer', 'critical', 'push'),

('wrong_password_threshold',
 'User exceeded wrong password attempt threshold',
 'SECURITY: Wrong Password Threshold: {username}',
 'User {username} has exceeded {attempts} wrong password attempts from IP {ip_address}.',
 'security', 'restricted', 'role', 'super_admin,admin', 'login', 'high', 'push'),


-- ---- USER MANAGEMENT (9 events) ----
('user_created_by_admin',
 'Admin created a new user account',
 'New User Created: {new_user_name}',
 'A new user account has been created for {new_user_name} ({new_user_email}) with role "{role_name}".',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac', 'medium', 'push'),

('user_invitation_sent',
 'Welcome email with setup link sent',
 'Invitation Sent: {user_name}',
 'An invitation email has been sent to {user_email} for user {user_name}.',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac', 'low', 'push'),

('user_invitation_resent',
 'Invitation re-sent',
 'Invitation Resent: {user_name}',
 'Invitation has been resent to {user_email} for user {user_name}.',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac', 'low', 'push'),

('user_token_verified',
 'User clicked email verification link',
 'User Verified: {user_name}',
 'User {user_name} ({user_email}) has verified their email.',
 'user_management', 'restricted', 'role', 'super_admin', 'rbac', 'low', 'push'),

('user_password_set',
 'User set their initial password',
 'Password Set: {user_name}',
 'User {user_name} has set their initial password and activated their account.',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac', 'medium', 'push'),

('user_account_enabled',
 'Previously disabled user re-enabled',
 'User Enabled: {user_name}',
 'User account for {user_name} has been re-enabled by {admin_name}.',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac', 'medium', 'push'),

('user_account_disabled',
 'User account deactivated',
 'User Disabled: {user_name}',
 'User account for {user_name} has been disabled by {admin_name}.',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac', 'high', 'push'),

('user_role_changed',
 'User role updated',
 'Role Changed: {user_name}',
 'User {user_name} role changed from "{old_role}" to "{new_role}" by {admin_name}.',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac', 'high', 'push'),

('user_updated',
 'User details updated',
 'User Updated: {user_name}',
 'User {user_name} profile has been updated. Fields: {fields}.',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac', 'low', 'push');
