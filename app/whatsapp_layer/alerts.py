"""In-app alerts to an org's admins (provider auth_error, high failure rate,
template rejected). Reuses the notification store + Redis fan-out exactly
like the scheduler does, deduplicated for 24 h per (org, key) in Redis.

Each alert is an Event (contract §6.2: system:alert_failure_rate,
system:alert_template_rejected, system:alert_account_error); a disabled
alert is not raised."""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

ALERT_DEDUPE_TTL = 24 * 3600


def org_admin_user_ids(db: Session, organization_id: int) -> List[int]:
    rows = db.execute(text(
        "SELECT u.id FROM users u JOIN roles r ON r.id = u.role_id "
        "WHERE u.organization_id = :org AND u.deleted_at IS NULL "
        "AND LOWER(r.name) IN ('admin', 'super_admin', 'super admin', 'superadmin')"
    ), {"org": organization_id}).fetchall()
    return [int(r[0]) for r in rows]


def _first_time(dedupe_key: Optional[str], organization_id: int) -> bool:
    if not dedupe_key:
        return True
    try:
        from app.notification_layer import redis_manager
        client = redis_manager.get_notification_redis()
        return bool(client.set(f"wa:alert:{organization_id}:{dedupe_key}", "1",
                               nx=True, ex=ALERT_DEDUPE_TTL))
    except Exception as exc:
        logger.warning("Alert dedupe unavailable (%s); sending anyway", exc)
        return True


def notify_org_admins(db: Session, organization_id: int, *, title: str,
                      message: str, dedupe_key: Optional[str] = None,
                      metadata: Optional[dict] = None,
                      event: Optional[str] = None) -> Optional[int]:
    """Create a push notification for the org's admins. Returns its id, or
    None when the `event` toggle is off / deduplicated / no admins / failed
    (never raises). `event` is the system event name, e.g.
    "alert_failure_rate"."""
    from app.notification_layer import redis_manager, store
    from app.whatsapp_layer import events as EV

    # Checked before dedupe so turning the alert back on isn't blocked for 24 h.
    if event and not EV.is_system_enabled(db, organization_id, event):
        logger.info("WhatsApp alert '%s' for org %s not raised: event %s is off",
                    title, organization_id, event)
        return None
    if not _first_time(dedupe_key, organization_id):
        return None
    try:
        admin_ids = org_admin_user_ids(db, organization_id)
        if not admin_ids:
            logger.warning("No admins found for org %s; alert '%s' dropped", organization_id, title)
            return None
        meta = dict(metadata or {})
        meta.update({"organization_id": organization_id, "source": "whatsapp"})
        notif, user_ids = store.create_notification(
            db=db, title=title, message=message,
            delivery_mode="push", domain_type="system", visibility="restricted",
            priority="high", target_type="user",
            target_id=",".join(str(i) for i in admin_ids),
            source_service="status", event_type="whatsapp_alert",
            metadata=meta, organization_id=organization_id,
        )
        payload = {
            "id": notif.id, "title": notif.title, "message": notif.message,
            "delivery_mode": notif.delivery_mode, "domain_type": notif.domain_type,
            "visibility": notif.visibility, "priority": notif.priority,
            "source_service": "status", "event_type": "whatsapp_alert",
            "metadata": meta, "created_at": str(notif.created_at),
        }
        redis_manager.invalidate_unread_count(user_ids)
        unread_counts = store.get_unread_counts_by_mode_bulk(db, user_ids)
        redis_manager.publish_to_users(user_ids, payload, unread_counts=unread_counts)
        logger.info("WhatsApp alert '%s' sent to %d admins of org %s",
                    title, len(user_ids), organization_id)
        return notif.id
    except Exception as exc:
        logger.error("WhatsApp alert '%s' for org %s failed: %s", title, organization_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None
