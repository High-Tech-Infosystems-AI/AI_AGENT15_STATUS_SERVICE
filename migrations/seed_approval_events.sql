-- ---------------------------------------------------------------------
-- Seed `notification_events` rows for the central Approval workflow.
--
-- Each event is fan-out via target_type='user' with a comma-separated
-- target_id of recipient user_ids supplied in `data.user_ids` by the
-- RBAC service's notifier (which enumerates org admins + super_admins
-- before firing).
--
-- Templates use {placeholder} which the event_handler renders from the
-- event payload (`data`).
-- ---------------------------------------------------------------------

INSERT IGNORE INTO notification_events
  (event_name, description, default_title_template, default_message_template,
   domain_type, visibility, target_type, target_roles, source_service,
   priority, delivery_mode, also_banner, is_enabled)
VALUES

  -- Admins/SAs: new approval request waiting for review
  ('approval_requested',
   'A tier user filed an approval request for a Company / Job create',
   'Approval needed: new {entity_type}',
   'A new {entity_type} approval request (#{request_id}) is waiting for your review.',
   'system', 'restricted', 'user', NULL, 'rbac',
   'high', 'push', 0, 1),

  -- Requester: their request was approved
  ('approval_approved',
   'A pending approval request was approved by an admin',
   'Your {entity_type} request was approved',
   'Approval request #{request_id} has been approved. {review_remarks}',
   'system', 'personal', 'user', NULL, 'rbac',
   'medium', 'push', 0, 1),

  -- Requester: their request was rejected
  ('approval_rejected',
   'A pending approval request was rejected by an admin',
   'Your {entity_type} request was rejected',
   'Approval request #{request_id} was rejected. Reason: {review_remarks}',
   'system', 'personal', 'user', NULL, 'rbac',
   'high', 'push', 0, 1),

  -- Requester: they cancelled their own request — used for log/audit only
  ('approval_cancelled',
   'A requester cancelled their pending approval before review',
   'You cancelled your {entity_type} request',
   'You cancelled approval request #{request_id}.',
   'system', 'personal', 'user', NULL, 'rbac',
   'low', 'log', 0, 1),

  -- Requester: their request expired without a decision
  ('approval_expired',
   'A pending approval request expired without review (SLA breach)',
   'Your {entity_type} request expired',
   'Approval request #{request_id} expired without review. You may resubmit.',
   'system', 'personal', 'user', NULL, 'rbac',
   'medium', 'push', 0, 1),

  -- Either party commented (audience set at send time via data.user_ids)
  ('approval_commented',
   'A comment was added to an approval thread',
   'New comment on approval #{request_id}',
   '{comment_excerpt}',
   'system', 'personal', 'user', NULL, 'rbac',
   'low', 'push', 0, 1);
