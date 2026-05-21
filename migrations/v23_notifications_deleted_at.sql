-- ============================================================================
-- Notification Service — V23 Migration
-- Adds a dedicated soft-delete marker to `notifications`.
--
-- Background: `is_active` was overloaded — it dropped to 0 both when a banner
-- EXPIRED and when a notification was DELETED. The History view wants expired
-- banners to remain visible (status="expired") while explicitly deleted rows
-- stay hidden. A single flag can't express both, so we add `deleted_at`:
--   - deleted_at IS NULL  → never deleted (may still be active or expired)
--   - deleted_at IS NOT  NULL → explicitly deleted; hide everywhere
--
-- After this migration, soft_delete_notification() stamps deleted_at, and
-- get_admin_notification_logs() filters deleted_at IS NULL so deleted-and-
-- expired banners no longer reappear.
--
-- Idempotent — safe to re-run (INFORMATION_SCHEMA checks for column + index).
-- ============================================================================

-- ---- 1. Add deleted_at column to notifications (idempotent) ----
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
          AND TABLE_NAME   = 'notifications'
          AND COLUMN_NAME  = col_name
    ) THEN
        SET @sql = CONCAT('ALTER TABLE notifications ADD COLUMN ', col_name, ' ', col_def);
        PREPARE stmt FROM @sql;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

CALL _notif_add_col('deleted_at',
    'DATETIME DEFAULT NULL COMMENT ''Set when a notification is explicitly soft-deleted; NULL = not deleted''');

DROP PROCEDURE _notif_add_col;


-- ---- 2. Add index on deleted_at (idempotent) ----
DROP PROCEDURE IF EXISTS _notif_add_idx;
DELIMITER $$
CREATE PROCEDURE _notif_add_idx()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'notifications'
          AND INDEX_NAME   = 'idx_deleted_at'
    ) THEN
        ALTER TABLE notifications ADD INDEX idx_deleted_at (deleted_at);
    END IF;
END$$
DELIMITER ;

CALL _notif_add_idx();

DROP PROCEDURE _notif_add_idx;


-- ---- 3. Backfill is intentionally NOT done ----
-- Existing is_active=0 rows are NOT marked deleted here: we can't tell which
-- were expired vs deleted retroactively, and treating them all as "expired"
-- (deleted_at IS NULL) matches the pre-existing History behavior. From now on,
-- only deletions via soft_delete_notification() set deleted_at.
