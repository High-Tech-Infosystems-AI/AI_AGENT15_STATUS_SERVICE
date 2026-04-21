-- ============================================================================
-- V5 Migration — Performance indexes for notification queries.
-- These indexes speed up the hot paths:
--   1. user notification list (join recipients on user + created_at sort)
--   2. admin logs (filter by date/domain/delivery_mode, sort by created_at)
--   3. unread count (user_id + is_read + notification.is_active + delivery_mode)
--   4. active banners (delivery_mode + is_active + expires_at)
-- All CREATE INDEX statements use IF NOT EXISTS so this is idempotent.
-- ============================================================================

-- MySQL 8.0+ supports IF NOT EXISTS on CREATE INDEX. If your version is 5.7
-- you'll need to drop any that already exist first.

-- 1. Notification recipients: the hot path for "my notifications"
--    Covers: WHERE user_id = ? AND is_read = 0 ORDER BY ...
CREATE INDEX /*!80000 IF NOT EXISTS */ idx_nr_user_read_created
    ON notification_recipients (user_id, is_read, created_at DESC);

-- 2. Notifications: admin logs filter/sort hot path
--    Covers: WHERE is_active = 1 AND delivery_mode != 'log' AND created_at BETWEEN x AND y
CREATE INDEX /*!80000 IF NOT EXISTS */ idx_notif_active_mode_created
    ON notifications (is_active, delivery_mode, created_at DESC);

-- 3. Notifications: active-banner lookup (per-user join via recipients)
--    Covers: delivery_mode='banner' AND is_active=1 AND expires_at > NOW()
CREATE INDEX /*!80000 IF NOT EXISTS */ idx_notif_banner_active_expires
    ON notifications (delivery_mode, is_active, expires_at);

-- 4. Notifications: domain/source/priority filter
CREATE INDEX /*!80000 IF NOT EXISTS */ idx_notif_domain_priority
    ON notifications (domain_type, priority, created_at DESC);

CREATE INDEX /*!80000 IF NOT EXISTS */ idx_notif_source_event
    ON notifications (source_service, event_type, created_at DESC);

-- 5. Notifications: target lookup (admin logs filter by target_type/target_id)
CREATE INDEX /*!80000 IF NOT EXISTS */ idx_notif_target
    ON notifications (target_type, target_id);

-- 6. Schedule lookup (scheduler runs every 60s)
CREATE INDEX /*!80000 IF NOT EXISTS */ idx_sched_status_at
    ON notification_schedules (status, scheduled_at);

-- 7. Notification events (for event lookup by name)
-- Usually primary key + unique(event_name) already covers this. Safe skip.


-- Analyze tables to refresh optimizer statistics
ANALYZE TABLE notifications;
ANALYZE TABLE notification_recipients;
ANALYZE TABLE notification_schedules;
ANALYZE TABLE notification_events;
