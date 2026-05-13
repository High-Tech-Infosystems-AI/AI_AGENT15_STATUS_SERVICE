-- v15_chat_forward_sender.sql
-- Denormalised "forwarded from" sender id. When user A forwards user B's
-- message into conversation X, we already store forwarded_from_message_id
-- (link to the original); this column additionally caches the original
-- sender's user_id so the recipient can render "Forwarded from <name>"
-- without a second lookup or join across the audit chain. Resolved through
-- the user_info_cache to get username/name.

ALTER TABLE chat_messages
    ADD COLUMN forwarded_from_sender_id INT NULL AFTER forwarded_from_message_id,
    ADD KEY idx_forwarded_from_sender (forwarded_from_sender_id);
