-- v17_chat_message_references.sql
-- Add structured entity references to chat messages. Each message can carry
-- any number of {type, id} references which are rendered as click-through
-- cards on the frontend. Body text holds opaque tokens like
-- @@ref:job:42@@ that the renderer replaces with the matching card.
--
-- We use a JSON column rather than a side table because:
--   * references are always read together with the message
--   * the structure is fluid (cards may add fields)
--   * search-by-reference isn't a query pattern we need

ALTER TABLE chat_messages
    ADD COLUMN refs JSON NULL AFTER body;

-- Add a flag so we can mark synthetic messages (e.g. Status Bot replies)
-- without conflating them with regular sender_id rows.
ALTER TABLE chat_messages
    ADD COLUMN is_system TINYINT(1) NOT NULL DEFAULT 0 AFTER message_type;
