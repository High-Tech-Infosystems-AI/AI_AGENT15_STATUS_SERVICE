-- v12_chat_general_seed.sql
-- Seed the singleton #general conversation (id=1).
INSERT IGNORE INTO chat_conversations (id, type, title, created_by, created_at)
VALUES (1, 'general', '#general', NULL, CURRENT_TIMESTAMP);
