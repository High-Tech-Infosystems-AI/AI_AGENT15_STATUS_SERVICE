"""
Background Notification Scheduler.

Runs every 60 seconds to:
1. Fire pending scheduled notifications
2. Check job deadlines (exceeded + approaching)
3. Expire banners past their expires_at
"""

import asyncio
import logging
from datetime import datetime, timedelta

from app.database_Layer.db_config import SessionLocal
from app.notification_layer import store, redis_manager
from app.notification_layer.event_handler import handle_event

logger = logging.getLogger("app_logger")

SCHEDULER_INTERVAL_SECONDS = 60
DEADLINE_APPROACHING_DAYS = 3


async def run_scheduler():
    """Main scheduler loop — runs as a background asyncio task."""
    logger.info("Notification scheduler started (interval=%ds)", SCHEDULER_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)

            # Acquire distributed lock to prevent multi-instance duplicates
            if not redis_manager.acquire_scheduler_lock():
                continue

            try:
                _process_tick()
            finally:
                redis_manager.release_scheduler_lock()

        except Exception as e:
            logger.error("Scheduler tick error: %s", e, exc_info=True)


def _process_tick():
    """Single scheduler tick — runs all checks inside a DB session."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        _fire_scheduled_notifications(db, now)
        _check_deadlines(db, now)
        _expire_banners(db)
    except Exception as e:
        logger.error("Scheduler processing error: %s", e, exc_info=True)
    finally:
        db.close()


def _fire_scheduled_notifications(db, now: datetime):
    """Send all pending schedules whose time has come."""
    pending = store.get_pending_schedules(db, now)
    for sched in pending:
        try:
            metadata = None
            if sched.extra_metadata:
                import json
                try:
                    metadata = json.loads(sched.extra_metadata) if isinstance(sched.extra_metadata, str) else sched.extra_metadata
                except (ValueError, TypeError):
                    metadata = None

            notif, user_ids = store.create_notification(
                db=db,
                title=sched.title,
                message=sched.message,
                delivery_mode=sched.delivery_mode,
                domain_type=sched.domain_type,
                visibility=sched.visibility,
                priority=sched.priority,
                target_type=sched.target_type,
                target_id=sched.target_id,
                source_service="system",
                event_type="scheduled_notification",
                metadata=metadata,
                created_by=sched.created_by,
            )

            # Publish
            pub_payload = {
                "id": notif.id,
                "title": notif.title,
                "message": notif.message,
                "delivery_mode": notif.delivery_mode,
                "domain_type": notif.domain_type,
                "visibility": notif.visibility,
                "priority": notif.priority,
                "source_service": "system",
                "event_type": "scheduled_notification",
                "metadata": metadata,
                "created_at": str(notif.created_at),
            }
            if notif.visibility == "public" or sched.target_type == "all":
                redis_manager.publish_broadcast(pub_payload)
            else:
                redis_manager.publish_to_users(user_ids, pub_payload)

            if notif.delivery_mode == "banner":
                redis_manager.publish_banner("create", pub_payload)
                redis_manager.invalidate_banner_cache()

            redis_manager.invalidate_unread_count(user_ids)

            # Update schedule status
            sched.last_sent_at = now
            if sched.repeat_type == "once":
                sched.status = "sent"
            elif sched.repeat_type == "daily":
                sched.scheduled_at = sched.scheduled_at + timedelta(days=1)
                if sched.repeat_until and sched.scheduled_at > sched.repeat_until:
                    sched.status = "sent"
            elif sched.repeat_type == "weekly":
                sched.scheduled_at = sched.scheduled_at + timedelta(weeks=1)
                if sched.repeat_until and sched.scheduled_at > sched.repeat_until:
                    sched.status = "sent"
            db.commit()

            logger.info("Scheduled notification %s fired → notification %s", sched.id, notif.id)

        except Exception as e:
            logger.error("Failed to fire schedule %s: %s", sched.id, e, exc_info=True)


def _check_deadlines(db, now: datetime):
    """Check for job deadlines exceeded and approaching."""
    today = now.date()
    approaching_date = today + timedelta(days=DEADLINE_APPROACHING_DAYS)

    # Deadlines exceeded today
    exceeded_jobs = store.get_jobs_with_deadline_today(db, today)
    for job in exceeded_jobs:
        try:
            handle_event(db, "job_deadline_exceeded", {
                "job_title": job.title,
                "job_id": job.id,
                "job_public_id": job.job_id,
                "deadline": str(job.deadline),
            })
        except Exception as e:
            logger.error("Deadline exceeded event for job %s failed: %s", job.id, e)

    # Deadlines approaching
    approaching_jobs = store.get_jobs_with_deadline_approaching(db, approaching_date)
    for job in approaching_jobs:
        try:
            handle_event(db, "job_deadline_approaching", {
                "job_title": job.title,
                "job_id": job.id,
                "job_public_id": job.job_id,
                "deadline": str(job.deadline),
                "days_remaining": DEADLINE_APPROACHING_DAYS,
            })
        except Exception as e:
            logger.error("Deadline approaching event for job %s failed: %s", job.id, e)


def _expire_banners(db):
    """Deactivate expired banners and notify connected clients."""
    expired_ids = store.deactivate_expired_banners(db)
    for banner_id in expired_ids:
        redis_manager.publish_banner("expire", {"id": banner_id})
    if expired_ids:
        redis_manager.invalidate_banner_cache()
