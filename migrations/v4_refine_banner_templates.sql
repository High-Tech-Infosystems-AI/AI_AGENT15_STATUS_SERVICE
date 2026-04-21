-- ============================================================================
-- V4 Migration — Refine banner templates for candidate_joined and clawback.
-- Makes it explicit WHO closed the job and WHICH candidate is involved.
-- Also aligns banner_expires_hours to 24 so daily-cycle logic in event_handler
-- (which floors to next midnight UTC) applies consistently.
-- Idempotent.
-- ============================================================================

-- candidate_joined (= job closed / position filled)
-- Banner now names BOTH the user who closed it AND the candidate.
UPDATE notification_events SET
    banner_title_template   = 'Job Closed: {job_title}',
    banner_message_template = '{user_name} closed one position for "{job_title}" — candidate {candidate_name} has joined. Congratulations to the team!',
    banner_expires_hours    = 24
WHERE event_name = 'candidate_joined';


-- clawback_marked_complete — banner names the candidate + the user who marked it
UPDATE notification_events SET
    banner_title_template   = 'Clawback Complete: {candidate_name}',
    banner_message_template = 'Clawback period for candidate {candidate_name} on job "{job_title}" is complete — marked by {user_name}.',
    banner_expires_hours    = 24
WHERE event_name = 'clawback_marked_complete';


-- candidate_rejected — also include candidate name + user in the banner
UPDATE notification_events SET
    banner_title_template   = 'Candidate Rejected: {candidate_name}',
    banner_message_template = 'Candidate {candidate_name} was rejected for job "{job_title}" by {user_name}. Reason: {reason}',
    banner_expires_hours    = 24
WHERE event_name = 'candidate_rejected';


-- Deadline events — also align to 24h
UPDATE notification_events SET banner_expires_hours = 24
WHERE event_name IN ('job_deadline_approaching', 'job_deadline_exceeded',
                     'job_deadline_updated',   'job_positions_updated');

-- v3 events that we added earlier
UPDATE notification_events SET banner_expires_hours = 24
WHERE event_name IN ('job_created', 'candidate_joined_updated', 'intimation_mail_sent');
