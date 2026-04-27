-- v11_chat_indexes.sql
-- FULLTEXT for search; conditional fallback handled in store layer.
ALTER TABLE chat_messages ADD FULLTEXT KEY ft_body (body);
ALTER TABLE chat_messages ADD KEY idx_conv_created_desc (conversation_id, created_at DESC, id DESC);
ALTER TABLE chat_conversations ADD KEY idx_last_message_desc (last_message_at DESC);
