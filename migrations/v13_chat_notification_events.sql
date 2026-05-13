-- v13_chat_notification_events.sql
INSERT IGNORE INTO notification_events
    (event_name, description, default_title_template, default_message_template,
     domain_type, visibility, target_type, source_service, priority, delivery_mode, is_enabled)
VALUES
    ('chat.message_received',
     'Triggered when a chat message is delivered to an offline recipient',
     'New message from {sender_name}',
     '{preview}',
     'chat', 'personal', 'user', 'chat', 'medium', 'push', 1),
    ('chat.mention',
     'Triggered when a user is @mentioned in a chat message',
     '{sender_name} mentioned you in {conversation_label}',
     '{preview}',
     'chat', 'personal', 'user', 'chat', 'high', 'push', 1);
