-- ============================================================================
-- V6 Migration — Rewrite closure/rejection/clawback templates.
--
-- Rules:
-- 1. NO candidate_name anywhere (primary notification OR banner).
-- 2. For candidate_joined (position closed):
--      - Primary notification → the recruiter who closed it gets a
--        congratulatory message.
--      - Banner (to job team + admins) → "{user_name} has closed a position
--        for {job_title}."
-- 3. For candidate_rejected and clawback_marked_complete: same pattern —
--    mention the user and job only, never the candidate.
--
-- Idempotent.
-- ============================================================================


-- candidate_joined = Position Closed
UPDATE notification_events SET
    default_title_template   = 'Position Closed: {job_title}',
    default_message_template = 'Congratulations! You have closed a position for "{job_title}". Great work!',
    banner_title_template    = 'Position Closed: {job_title}',
    banner_message_template  = '{user_name} has closed a position for "{job_title}".',
    banner_expires_hours     = 24
WHERE event_name = 'candidate_joined';


-- candidate_rejected — user + job only, no candidate name
UPDATE notification_events SET
    default_title_template   = 'Rejection Recorded: {job_title}',
    default_message_template = 'A candidate was rejected for "{job_title}" by {user_name}. Reason: {reason}',
    banner_title_template    = 'Rejection Recorded: {job_title}',
    banner_message_template  = '{user_name} recorded a rejection for "{job_title}". Reason: {reason}',
    banner_expires_hours     = 24
WHERE event_name = 'candidate_rejected';


-- clawback_marked_complete — user + job only
UPDATE notification_events SET
    default_title_template   = 'Clawback Complete: {job_title}',
    default_message_template = 'You have marked clawback as complete for "{job_title}". Great work!',
    banner_title_template    = 'Clawback Complete: {job_title}',
    banner_message_template  = '{user_name} marked clawback as complete for "{job_title}".',
    banner_expires_hours     = 24
WHERE event_name = 'clawback_marked_complete';


-- Also drop candidate_name from other related events that previously had it
UPDATE notification_events SET
    default_title_template   = 'Joining Updated: {job_title}',
    default_message_template = 'Joining details for job "{job_title}" were updated by {user_name}.',
    banner_title_template    = 'Joining Updated: {job_title}',
    banner_message_template  = '{user_name} updated joining details for "{job_title}".'
WHERE event_name = 'candidate_joined_updated';

UPDATE notification_events SET
    default_title_template   = 'Rejection Updated: {job_title}',
    default_message_template = 'Rejection details for job "{job_title}" were updated by {user_name}.'
WHERE event_name = 'candidate_rejected_updated';

UPDATE notification_events SET
    default_title_template   = 'Remark Added: {job_title}',
    default_message_template = '{user_name} added a remark for a joined candidate on "{job_title}".'
WHERE event_name = 'candidate_joined_remark_added';

UPDATE notification_events SET
    default_title_template   = 'Remark Added: {job_title}',
    default_message_template = '{user_name} added a remark for a rejected candidate on "{job_title}".'
WHERE event_name = 'candidate_rejected_remark_added';
