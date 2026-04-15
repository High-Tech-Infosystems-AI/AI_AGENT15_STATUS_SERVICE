"""
Auto-Notification Event Handler.

Processes event triggers from other microservices:
1. Looks up the event config in notification_events table
2. Renders title/message from templates + data
3. Resolves target users based on event config + data
4. Creates the notification(s) + recipients — primary + optional banner
5. Publishes to Redis for real-time delivery (type: notification | banner | log)
6. Invalidates unread caches
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

from sqlalchemy.orm import Session

from app.notification_layer import store, redis_manager
from app.notification_layer.store import TargetValidationError
from app.notification_layer.models import NotificationEvent, Notification

logger = logging.getLogger("app_logger")

# Default banner TTL when event config doesn't override it
DEFAULT_BANNER_EXPIRES_HOURS = 24


def _render_template(template: str, data: dict) -> str:
    """Render a template string with data dict using str.format_map with fallback."""
    try:
        class SafeDict(dict):
            def __missing__(self, key):
                return f"{{{key}}}"
        return template.format_map(SafeDict(data))
    except Exception as e:
        logger.warning("Template render error: %s (template=%s)", e, template)
        return template


def _determine_target(
    target_type: str,
    target_roles: Optional[str],
    data: dict,
) -> Tuple[str, Optional[str]]:
    """
    Resolve final (target_type, target_id) for a given configured target.

    The event config has a default target_type (role, job, user, all).
    The data dict can override/provide specific IDs:
      - data.user_id  → personal notification to that user
      - data.job_id   → job-targeted notification
      - data.user_ids → csv of user IDs
    """
    target_id = None

    if target_type == "role":
        target_id = target_roles  # csv of role names

    elif target_type == "user":
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


def _publish_notification(
    notif: Notification,
    user_ids: List[int],
    event_name: str,
    metadata: dict,
    unread_counts: dict,
) -> None:
    """Publish a push/log notification to Redis for WS fan-out."""
    payload = {
        "id": notif.id,
        "title": notif.title,
        "message": notif.message,
        "delivery_mode": notif.delivery_mode,  # push | log — ws_manager routes on this
        "domain_type": notif.domain_type,
        "visibility": notif.visibility,
        "priority": notif.priority,
        "source_service": notif.source_service,
        "event_type": event_name,
        "metadata": metadata,
        "created_at": str(notif.created_at),
    }

    if notif.visibility == "public" or notif.target_type == "all":
        redis_manager.publish_broadcast(payload, user_unread_counts=unread_counts)
    else:
        redis_manager.publish_to_users(user_ids, payload, unread_counts=unread_counts)


def _publish_banner(
    notif: Notification,
    user_ids: List[int],
    event_name: str,
    metadata: dict,
    db: Optional[Session] = None,
) -> None:
    """Publish a banner to Redis for WS fan-out. Routed per-recipient by ws_manager.
    Also publishes per-user banner snapshots so clients have full current state.
    """
    payload = {
        "id": notif.id,
        "title": notif.title,
        "message": notif.message,
        "delivery_mode": "banner",
        "domain_type": notif.domain_type,
        "visibility": notif.visibility,
        "priority": notif.priority,
        "source_service": notif.source_service,
        "event_type": event_name,
        "metadata": metadata,
        "expires_at": str(notif.expires_at) if notif.expires_at else None,
        "created_at": str(notif.created_at),
        "recipient_ids": user_ids,  # ws_manager strips this before forwarding
    }
    redis_manager.publish_banner("create", payload)
    redis_manager.invalidate_banner_cache()

    # Publish full updated snapshots to each affected user
    if db is not None and user_ids:
        snapshots = store.get_active_banners_for_users_bulk(db, user_ids)
        redis_manager.publish_banner_snapshots(snapshots)


def _create_and_publish(
    db: Session,
    *,
    event_config: NotificationEvent,
    event_name: str,
    title: str,
    message: str,
    delivery_mode: str,
    target_type: str,
    target_id: Optional[str],
    metadata: dict,
    expires_at: Optional[datetime] = None,
) -> Tuple[Notification, List[int]]:
    """Create one notification row + recipients; caller does the publishing."""
    notif, user_ids = store.create_notification(
        db=db,
        title=title,
        message=message,
        delivery_mode=delivery_mode,
        domain_type=event_config.domain_type,
        visibility=event_config.visibility,
        priority=event_config.priority,
        target_type=target_type,
        target_id=target_id,
        source_service=event_config.source_service,
        event_type=event_name,
        metadata=metadata,
        expires_at=expires_at,
    )
    return notif, user_ids


def handle_event(
    db: Session,
    event_name: str,
    data: dict,
) -> Tuple[bool, Optional[int], str]:
    """
    Process an auto-notification event.

    Returns: (success, primary_notification_id_or_None, message)
    """
    # 1. Look up event config
    event_config = store.get_event_config(db, event_name)
    if not event_config:
        logger.warning("Unknown or disabled event: %s", event_name)
        return False, None, f"Event '{event_name}' not found or disabled"

    metadata = data.copy() if data else {}

    # 2. Primary delivery (push | banner | log) ---------------------------------
    title = _render_template(event_config.default_title_template, data)
    message = _render_template(event_config.default_message_template, data)
    target_type, target_id = _determine_target(
        event_config.target_type, event_config.target_roles, data,
    )

    primary_expires = None
    if event_config.delivery_mode == "banner":
        hours = event_config.banner_expires_hours or DEFAULT_BANNER_EXPIRES_HOURS
        primary_expires = datetime.utcnow() + timedelta(hours=hours)

    try:
        primary_notif, primary_user_ids = _create_and_publish(
            db,
            event_config=event_config,
            event_name=event_name,
            title=title,
            message=message,
            delivery_mode=event_config.delivery_mode,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
            expires_at=primary_expires,
        )
    except TargetValidationError as e:
        db.rollback()
        logger.warning(
            "Event '%s' skipped: %s (target_type=%s, target_id=%s)",
            event_name, e.message, target_type, target_id,
        )
        return False, None, f"Validation failed: {e.message}"

    # Invalidate unread caches BEFORE computing fresh counts
    # (logs don't count toward unread, but we invalidate defensively for push)
    if event_config.delivery_mode == "push":
        redis_manager.invalidate_unread_count(primary_user_ids)

    # Per-mode breakdown so WS clients receive {push, banner, log, total}
    unread_counts = store.get_unread_counts_by_mode_bulk(db, primary_user_ids)

    # Dispatch to the right Redis channel based on delivery_mode
    if event_config.delivery_mode == "banner":
        _publish_banner(primary_notif, primary_user_ids, event_name, metadata, db=db)
    else:
        # push OR log — both go through the per-user/broadcast channel;
        # ws_manager emits type="notification" or type="log" based on
        # the delivery_mode field embedded in the payload.
        _publish_notification(
            primary_notif, primary_user_ids, event_name, metadata, unread_counts,
        )

    # 3. Optional secondary banner ----------------------------------------------
    banner_id = None
    if event_config.also_banner:
        try:
            banner_title = _render_template(
                event_config.banner_title_template or event_config.default_title_template,
                data,
            )
            banner_message = _render_template(
                event_config.banner_message_template or event_config.default_message_template,
                data,
            )

            # Resolve banner's own target (may differ from primary)
            b_target_type_cfg = event_config.banner_target_type or event_config.target_type
            b_target_roles_cfg = (
                event_config.banner_target_roles
                if event_config.banner_target_type
                else event_config.target_roles
            )
            b_target_type, b_target_id = _determine_target(
                b_target_type_cfg, b_target_roles_cfg, data,
            )

            hours = event_config.banner_expires_hours or DEFAULT_BANNER_EXPIRES_HOURS
            banner_expires = datetime.utcnow() + timedelta(hours=hours)

            banner_notif, banner_user_ids = _create_and_publish(
                db,
                event_config=event_config,
                event_name=event_name,
                title=banner_title,
                message=banner_message,
                delivery_mode="banner",
                target_type=b_target_type,
                target_id=b_target_id,
                metadata=metadata,
                expires_at=banner_expires,
            )
            banner_id = banner_notif.id
            _publish_banner(banner_notif, banner_user_ids, event_name, metadata, db=db)

            logger.info(
                "Event '%s' → banner %s sent to %d users (also_banner)",
                event_name, banner_notif.id, len(banner_user_ids),
            )
        except TargetValidationError as e:
            # Banner failure doesn't abort the primary — just log it
            db.rollback()
            logger.warning(
                "Event '%s' banner skipped: %s (target_type=%s)",
                event_name, e.message, event_config.banner_target_type,
            )

    logger.info(
        "Event '%s' → notification %s (mode=%s) sent to %d users%s",
        event_name, primary_notif.id, event_config.delivery_mode, len(primary_user_ids),
        f"; banner {banner_id}" if banner_id else "",
    )
    return True, primary_notif.id, f"Notification sent to {len(primary_user_ids)} users"
