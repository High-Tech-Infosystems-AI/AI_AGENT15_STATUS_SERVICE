-- v21_chat_tasks_message_nonunique.sql
-- Allow a single chat_messages row to spawn multiple chat_tasks rows
-- (the new multi-task composer creates a "task list" — one message,
-- N tasks each with its own assignees / due / priority). The original
-- v20 migration declared `chat_tasks.message_id BIGINT NOT NULL UNIQUE`
-- which blocked that. Here we drop the unique index and add a plain
-- INDEX so message_id lookups stay fast.

-- Drop the auto-generated UNIQUE index named `message_id`, if present.
SET @has_unique := (
  SELECT COUNT(*) FROM information_schema.statistics
   WHERE table_schema = DATABASE()
     AND table_name   = 'chat_tasks'
     AND index_name   = 'message_id'
     AND non_unique   = 0
);
SET @sql := IF(@has_unique > 0,
  'ALTER TABLE chat_tasks DROP INDEX message_id',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Idempotently add a non-unique helper index for joins / filters.
SET @has_idx := (
  SELECT COUNT(*) FROM information_schema.statistics
   WHERE table_schema = DATABASE()
     AND table_name   = 'chat_tasks'
     AND index_name   = 'idx_task_message'
);
SET @sql := IF(@has_idx = 0,
  'CREATE INDEX idx_task_message ON chat_tasks (message_id)',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
