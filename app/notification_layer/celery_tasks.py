"""
Celery tasks for the Notification Scheduler.

Replaces the asyncio scheduler loop — these are triggered by Celery Beat.
Each task acquires a Redis lock to prevent duplicate execution in multi-instance deployments.
"""

import json
import logging
from datetime import datetime, timedelta

from app.notification_layer.celery_app import celery_app
from app.notification_layer import store, redis_manager
from app.notification_layer.event_handler import handle_event
from app.database_Layer.db_config import SessionLocal

logger = logging.getLogger("app_logger")

DEADLINE_APPROACHING_DAYS = 3


@celery_app.task(name="app.notification_layer.celery_tasks.fire_scheduled_notifications", bind=True)
def fire_scheduled_notifications(self):
    """Fire all pending scheduled notifications whose time has come."""
    if not redis_manager.acquire_scheduler_lock("notif:lock:scheduled", ttl=55):
        logger.debug("Skipping fire_scheduled_notifications — another instance holds the lock")
        return "skipped"

    db = SessionLocal()
    fired = 0
    try:
        now = datetime.utcnow()
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

                # Publish to Redis for real-time delivery
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
                redis_manager.invalidate_unread_count(user_ids)
                unread_counts = store.get_unread_counts_bulk(db, user_ids)

                if notif.visibility == "public" or sched.target_type == "all":
                    redis_manager.publish_broadcast(pub_payload, user_unread_counts=unread_counts)
                else:
                    redis_manager.publish_to_users(user_ids, pub_payload, unread_counts=unread_counts)

                if notif.delivery_mode == "banner":
                    redis_manager.publish_banner("create", pub_payload)
                    redis_manager.invalidate_banner_cache()

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
                fired += 1

                logger.info("Scheduled notification %s fired → notification %s", sched.id, notif.id)

            except Exception as e:
                logger.error("Failed to fire schedule %s: %s", sched.id, e, exc_info=True)
                db.rollback()

    except Exception as e:
        logger.error("fire_scheduled_notifications error: %s", e, exc_info=True)
    finally:
        db.close()
        redis_manager.release_scheduler_lock("notif:lock:scheduled")

    return f"fired={fired}"


@celery_app.task(name="app.notification_layer.celery_tasks.check_job_deadlines", bind=True)
def check_job_deadlines(self):
    """Check for job deadlines exceeded and approaching."""
    if not redis_manager.acquire_scheduler_lock("notif:lock:deadlines", ttl=3500):
        logger.debug("Skipping check_job_deadlines — another instance holds the lock")
        return "skipped"

    db = SessionLocal()
    try:
        now = datetime.utcnow()
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

    except Exception as e:
        logger.error("check_job_deadlines error: %s", e, exc_info=True)
    finally:
        db.close()
        redis_manager.release_scheduler_lock("notif:lock:deadlines")

    return f"exceeded={len(exceeded_jobs)}, approaching={len(approaching_jobs)}"


@celery_app.task(name="app.notification_layer.celery_tasks.expire_banners", bind=True)
def expire_banners(self):
    """Deactivate expired banners and notify connected clients."""
    if not redis_manager.acquire_scheduler_lock("notif:lock:banners", ttl=55):
        return "skipped"

    db = SessionLocal()
    try:
        expired_ids = store.deactivate_expired_banners(db)
        for banner_id in expired_ids:
            redis_manager.publish_banner("expire", {"id": banner_id})
        if expired_ids:
            redis_manager.invalidate_banner_cache()
        return f"expired={len(expired_ids)}"
    except Exception as e:
        logger.error("expire_banners error: %s", e, exc_info=True)
        return f"error={str(e)}"
    finally:
        db.close()
        redis_manager.release_scheduler_lock("notif:lock:banners")
