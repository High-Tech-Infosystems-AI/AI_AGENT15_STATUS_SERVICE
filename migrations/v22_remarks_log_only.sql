-- ============================================================================
-- V22 Migration — Remarks are logs, not push notifications.
--
-- Adding a remark to a candidate (whether via a job context or directly on
-- the candidate) is informational, not actionable. Per product: don't push
-- a notification — just record it as a log entry so it shows up in activity
-- history without buzzing anyone.
--
-- Events affected:
--   candidate_joined_remark_added    (Job Service, joined-status remarks)
--   candidate_rejected_remark_added  (Job Service, rejected-status remarks)
--   candidate_remark_added           (Candidate Service, direct candidate remark)
--
-- delivery_mode='log' still flows through the same Redis channel; ws_manager
-- emits type="log" instead of type="notification", and logs are excluded
-- from unread counts in store.get_unread_counts_by_mode_bulk.
--
-- Idempotent.
-- ============================================================================

UPDATE notification_events SET
    delivery_mode = 'log',
    also_banner   = 0
WHERE event_name IN (
    'candidate_joined_remark_added',
    'candidate_rejected_remark_added',
    'candidate_remark_added'
);
