-- ============================================================================
-- V7 Migration — Add notification events for Team operations.
--
-- Covers:
--   RBAC Service:
--     team_created            — admin created a team
--     team_updated            — admin changed team name/desc/status
--     team_deleted            — admin deleted a team
--     team_members_added      — users added to a team
--     team_members_removed    — users removed from a team
--
--   Job Service:
--     team_assigned_to_job    — team(s) assigned to a job
--     team_removed_from_job   — team(s) removed from a job
--
-- Idempotent — uses ON DUPLICATE KEY UPDATE so safe to re-run.
-- ============================================================================

INSERT INTO notification_events
    (event_name, description, default_title_template, default_message_template,
     domain_type, visibility, target_type, target_roles, source_service,
     priority, delivery_mode,
     also_banner, banner_title_template, banner_message_template,
     banner_target_type, banner_target_roles, banner_expires_hours)
VALUES

-- ---------------------- RBAC ----------------------

('team_created',
 'Admin created a new team',
 'New Team: {team_name}',
 '{user_name} created a new team "{team_name}".',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac',
 'medium', 'push',
 1,
 'New Team: {team_name}',
 '{user_name} just created a new team "{team_name}".',
 'role', 'super_admin,admin', 24),

('team_updated',
 'Team details updated',
 'Team Updated: {team_name}',
 '{user_name} updated team "{team_name}".',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac',
 'low', 'push',
 0, NULL, NULL, NULL, NULL, NULL),

('team_deleted',
 'Team deleted',
 'Team Deleted: {team_name}',
 '{user_name} deleted team "{team_name}".',
 'user_management', 'restricted', 'role', 'super_admin,admin', 'rbac',
 'high', 'push',
 1,
 'Team Deleted: {team_name}',
 '{user_name} deleted team "{team_name}".',
 'role', 'super_admin,admin', 24),

('team_members_added',
 'Users added to a team',
 'Added to Team: {team_name}',
 'You have been added to team "{team_name}" by {user_name}.',
 'user_management', 'restricted', 'user', NULL, 'rbac',
 'medium', 'push',
 0, NULL, NULL, NULL, NULL, NULL),

('team_members_removed',
 'Users removed from a team',
 'Removed from Team: {team_name}',
 'You have been removed from team "{team_name}" by {user_name}.',
 'user_management', 'restricted', 'user', NULL, 'rbac',
 'medium', 'push',
 0, NULL, NULL, NULL, NULL, NULL),

-- ---------------------- JOB SERVICE ----------------------

('team_assigned_to_job',
 'Team(s) assigned to a job',
 'Team Assigned: {job_title}',
 'Your team "{team_name}" has been assigned to job "{job_title}" by {user_name}.',
 'jobs', 'restricted', 'user', NULL, 'job',
 'medium', 'push',
 1,
 'Team Assigned: {job_title}',
 '{user_name} assigned team "{team_name}" to job "{job_title}".',
 'role', 'super_admin,admin', 24),

('team_removed_from_job',
 'Team(s) removed from a job',
 'Team Removed: {job_title}',
 'Your team "{team_name}" has been removed from job "{job_title}" by {user_name}.',
 'jobs', 'restricted', 'user', NULL, 'job',
 'medium', 'push',
 0, NULL, NULL, NULL, NULL, NULL)

ON DUPLICATE KEY UPDATE
    description              = VALUES(description),
    default_title_template   = VALUES(default_title_template),
    default_message_template = VALUES(default_message_template),
    domain_type              = VALUES(domain_type),
    visibility               = VALUES(visibility),
    target_type              = VALUES(target_type),
    target_roles             = VALUES(target_roles),
    source_service           = VALUES(source_service),
    priority                 = VALUES(priority),
    delivery_mode            = VALUES(delivery_mode),
    also_banner              = VALUES(also_banner),
    banner_title_template    = VALUES(banner_title_template),
    banner_message_template  = VALUES(banner_message_template),
    banner_target_type       = VALUES(banner_target_type),
    banner_target_roles      = VALUES(banner_target_roles),
    banner_expires_hours     = VALUES(banner_expires_hours);
