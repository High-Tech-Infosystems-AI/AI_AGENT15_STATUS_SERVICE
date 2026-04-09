"""
Auto-Notification Event Handler.

Processes event triggers from other microservices:
1. Looks up the event config in notification_events table
2. Renders title/message from templates + data
3. Resolves target users based on event config + data
4. Creates the notification + recipients
5. Publishes to Redis for real-time delivery
6. Invalidates unread caches
"""

import logging
from typing import Optional, Tuple, List

from sqlalchemy.orm import Session

from app.notification_layer import store, redis_manager
from app.notification_layer.models import NotificationEvent, Notification

logger = logging.getLogger("app_logger")


def _render_template(template: str, data: dict) -> str:
    """Render a template string with data dict using str.format_map with fallback."""
    try:
        # Use a defaultdict-like approach so missing keys don't crash
        class SafeDict(dict):
            def __missing__(self, key):
                return f"{{{key}}}"
        return template.format_map(SafeDict(data))
    except Exception as e:
        logger.warning("Template render error: %s (template=%s)", e, template)
        return template


def _determine_target(event: NotificationEvent, data: dict) -> Tuple[str, Optional[str]]:
    """
    Determine the final target_type and target_id based on event config + data.

    The event config has a default target_type (role, job, user, all).
    The data dict can override/provide specific IDs:
      - data.user_id  → personal notification to that user
      - data.job_id   → job-targeted notification
      - data.user_ids → csv of user IDs
    """
    target_type = event.target_type
    target_id = None

    if target_type == "role":
        target_id = event.target_roles  # csv of role names

    elif target_type == "user":
        # Expect user_id or user_ids in data
        if "user_ids" in data:
            target_id = str(data["user_ids"])
        elif "user_id" in data:
            target_id = str(data["user_id"])

    elif target_type == "job":
        if "job_id" in data:
            target_id = str(data["job_id"])

    elif target_type == "all":
        target_id = None

    return target_type, target_id


def handle_event(
    db: Session,
    event_name: str,
    data: dict,
) -> Tuple[bool, Optional[int], str]:
    """
    Process an auto-notification event.

    Returns: (success, notification_id_or_None, message)
    """
    # 1. Look up event config
    event_config = store.get_event_config(db, event_name)
    if not event_config:
        logger.warning("Unknown or disabled event: %s", event_name)
        return False, None, f"Event '{event_name}' not found or disabled"

    # 2. Render title and message
    title = _render_template(event_config.default_title_template, data)
    message = _render_template(event_config.default_message_template, data)

    # 3. Determine target
    target_type, target_id = _determine_target(event_config, data)

    # 4. Build metadata from data (include everything the caller sent)
    metadata = data.copy() if data else {}

    # 5. Create the notification + recipients
    notif, user_ids = store.create_notification(
        db=db,
        title=title,
        message=message,
        delivery_mode=event_config.delivery_mode,
        domain_type=event_config.domain_type,
        visibility=event_config.visibility,
        priority=event_config.priority,
        target_type=target_type,
        target_id=target_id,
        source_service=event_config.source_service,
        event_type=event_name,
        metadata=metadata,
    )

    # 6. Publish to Redis for real-time delivery
    pub_payload = {
        "id": notif.id,
        "title": notif.title,
        "message": notif.message,
        "delivery_mode": notif.delivery_mode,
        "domain_type": notif.domain_type,
        "visibility": notif.visibility,
        "priority": notif.priority,
        "source_service": notif.source_service,
        "event_type": event_name,
        "metadata": metadata,
        "created_at": str(notif.created_at),
    }

    if notif.visibility == "public" or target_type == "all":
        redis_manager.publish_broadcast(pub_payload)
    else:
        redis_manager.publish_to_users(user_ids, pub_payload)

    if notif.delivery_mode == "banner":
        redis_manager.publish_banner("create", pub_payload)
        redis_manager.invalidate_banner_cache()

    # 7. Invalidate unread caches
    redis_manager.invalidate_unread_count(user_ids)

    logger.info(
        "Event '%s' → notification %s sent to %d users",
        event_name, notif.id, len(user_ids),
    )
    return True, notif.id, f"Notification sent to {len(user_ids)} users"
