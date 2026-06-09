-- ---------------------------------------------------------------------
-- Split the legacy single-row #general ("all group") conversation into
-- one row per organisation.
--
-- Idempotent: re-running adds only missing org rows; existing per-org
-- rows are left as-is. Members are remapped to their org's row.
--
-- Background:
--   The original implementation hard-coded conversation id=1 of type
--   'general' as the platform-wide #general. Every newly created user
--   was auto-joined to it (`ensure_general_member`). That broke tenant
--   isolation — Org A's users could read Org B's messages.
--
--   The new app code looks up #general by (type='general', org_id=$X)
--   and creates a fresh row on first access if none exists. This
--   migration retro-actively creates those org rows so existing users
--   land in the correct tenant scope without losing membership state.
-- ---------------------------------------------------------------------

START TRANSACTION;

-- 1. Create one #general row per organisation that has at least one
--    user but no existing per-org #general yet.
INSERT INTO chat_conversations (organization_id, type, title, created_at)
SELECT o.id,
       'general',
       CONCAT('#general (org ', o.id, ')'),
       NOW()
FROM organizations o
WHERE o.id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM chat_conversations c
    WHERE c.type = 'general' AND c.organization_id = o.id
  )
  AND EXISTS (
    SELECT 1 FROM users u
    WHERE u.organization_id = o.id AND u.deleted_at IS NULL
  );

-- 2. Re-home each user's #general membership to their org's row.
--    Skips users with NULL organization_id (super_admin platform-only).
INSERT IGNORE INTO chat_conversation_members
  (conversation_id, user_id, role_in_conversation, joined_at, muted, archived)
SELECT new_c.id, u.id, 'member', NOW(), 0, 0
FROM users u
JOIN chat_conversations new_c
  ON new_c.type = 'general'
 AND new_c.organization_id = u.organization_id
WHERE u.organization_id IS NOT NULL
  AND u.deleted_at IS NULL;

-- 3. Drop the cross-org rows that point at the legacy platform-wide
--    conversation. We only delete rows where the user's org now has
--    its OWN #general row; the legacy id=1 conversation itself is
--    kept (used as fallback for users with no org).
DELETE m
FROM chat_conversation_members m
JOIN chat_conversations old_c ON old_c.id = m.conversation_id
JOIN users u ON u.id = m.user_id
WHERE old_c.type = 'general'
  AND old_c.organization_id IS NULL
  AND u.organization_id IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM chat_conversations new_c
    WHERE new_c.type = 'general'
      AND new_c.organization_id = u.organization_id
  );

COMMIT;
