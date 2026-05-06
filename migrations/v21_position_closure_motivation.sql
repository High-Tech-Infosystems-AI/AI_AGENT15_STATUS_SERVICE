-- ============================================================================
-- V21 Migration — Position-closure templates carry motivation + closure count.
--
-- For event `candidate_joined` (a position is closed):
--   - Primary notification (to the closer, first-person)
--       Title:   "🎉 Position Closed: {job_title}"
--       Message: "Congratulations! You just closed the position
--                "{job_title}" — that's {closure_count_text} closed by you
--                so far. {motivation}"
--
--   - Banner (to job team + admins/super_admins)
--       Title:   "Position Closed: {job_title}"
--       Message: "🎉 {user_name} just closed a position for "{job_title}" —
--                {closure_count_text} closed in total. {motivation}
--                Let's congratulate them!"
--
-- New placeholders (sent by Job Service):
--   {motivation}            — randomly picked motivational line
--   {closure_count}         — int, closer's lifetime closures (incl. this one)
--   {closure_count_text}    — "1 position" / "N positions"
--
-- Notification service uses SafeDict, so older Job-Service builds that don't
-- send these fields will render literal "{motivation}" etc. — keep both ends
-- in lockstep when deploying.
--
-- Idempotent.
-- ============================================================================

UPDATE notification_events SET
    default_title_template   = '🎉 Position Closed: {job_title}',
    default_message_template = 'Congratulations! You just closed the position "{job_title}" — that''s {closure_count_text} closed by you so far. {motivation}',
    banner_title_template    = 'Position Closed: {job_title}',
    banner_message_template  = '🎉 {user_name} just closed a position for "{job_title}" — {closure_count_text} closed in total. {motivation} Let''s congratulate them!',
    banner_expires_hours     = 24
WHERE event_name = 'candidate_joined';
