"""
Notification Scheduler — dual mode.

Mode 1 (default): Asyncio background loop inside the FastAPI process.
         Used when Celery Beat is NOT running.

Mode 2 (production): Celery Beat triggers tasks in celery_tasks.py.
         When Celery Beat is active, the asyncio loop detects the lock
         is held and skips — no double execution.

The asyncio loop is kept as a fallback so the service works
without Celery in development / single-container deployments.
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
    """Asyncio fallback scheduler — skips if Celery Beat holds the lock."""
    logger.info("Notification asyncio scheduler started (interval=%ds, fallback mode)", SCHEDULER_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)

            # Try to acquire lock — if Celery Beat already holds it, skip
            if not redis_manager.acquire_scheduler_lock("notif:lock:scheduled", ttl=55):
                continue

            try:
                _process_tick()
            finally:
                redis_manager.release_scheduler_lock("notif:lock:scheduled")

        except Exception as e:
            logger.error("Scheduler tick error: %s", e, exc_info=True)


def _process_tick():
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
    import json
    pending = store.get_pending_schedules(db, now)
    for sched in pending:
        try:
            metadata = None
            if sched.extra_metadata:
                try:
                    metadata = json.loads(sched.extra_metadata) if isinstance(sched.extra_metadata, str) else sched.extra_metadata
                except (ValueError, TypeError):
                    metadata = None

            notif, user_ids = store.create_notification(
                db=db, title=sched.title, message=sched.message,
                delivery_mode=sched.delivery_mode, domain_type=sched.domain_type,
                visibility=sched.visibility, priority=sched.priority,
                target_type=sched.target_type, target_id=sched.target_id,
                source_service="system", event_type="scheduled_notification",
                metadata=metadata, created_by=sched.created_by,
            )

            pub_payload = {
                "id": notif.id, "title": notif.title, "message": notif.message,
                "delivery_mode": notif.delivery_mode, "domain_type": notif.domain_type,
                "visibility": notif.visibility, "priority": notif.priority,
                "source_service": "system", "event_type": "scheduled_notification",
                "metadata": metadata, "created_at": str(notif.created_at),
            }
            redis_manager.invalidate_unread_count(user_ids)
            unread_counts = store.get_unread_counts_bulk(db, user_ids)

            if notif.visibility == "public" or sched.target_type == "all":
                redis_manager.publish_broadcast(pub_payload, user_unread_counts=unread_counts)
            else:
                redis_manager.publish_to_users(user_ids, pub_payload, unread_counts=unread_counts)

            if notif.delivery_mode == "banner":
                redis_manager.publish_banner("create", pub_payload)
                redis_manager.invalidate_banner_cache()

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
    today = now.date()
    approaching_date = today + timedelta(days=DEADLINE_APPROACHING_DAYS)

    for job in store.get_jobs_with_deadline_today(db, today):
        try:
            handle_event(db, "job_deadline_exceeded", {
                "job_title": job.title, "job_id": job.id,
                "job_public_id": job.job_id, "deadline": str(job.deadline),
            })
        except Exception as e:
            logger.error("Deadline exceeded event for job %s failed: %s", job.id, e)

    for job in store.get_jobs_with_deadline_approaching(db, approaching_date):
        try:
            handle_event(db, "job_deadline_approaching", {
                "job_title": job.title, "job_id": job.id,
                "job_public_id": job.job_id, "deadline": str(job.deadline),
                "days_remaining": DEADLINE_APPROACHING_DAYS,
            })
        except Exception as e:
            logger.error("Deadline approaching event for job %s failed: %s", job.id, e)


def _expire_banners(db):
    """Deactivate expired banners and notify their original recipients with full snapshot."""
    expired = store.deactivate_expired_banners_with_recipients(db)
    if not expired:
        return

    # Send the legacy 'expire' event for backward-compat clients
    affected_users = set()
    for banner_id, recipient_ids in expired:
        redis_manager.publish_banner("expire", {
            "id": banner_id,
            "recipient_ids": list(recipient_ids),
        })
        affected_users.update(recipient_ids)

    # Publish full updated snapshot so each affected user has the
    # complete current active-banner list (without the expired ones).
    if affected_users:
        snapshots = store.get_active_banners_for_users_bulk(db, list(affected_users))
        redis_manager.publish_banner_snapshots(snapshots)

    redis_manager.invalidate_banner_cache()
