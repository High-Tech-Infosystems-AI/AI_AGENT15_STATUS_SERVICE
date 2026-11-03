"""
Celery application for the Notification Service.

Used for:
- Periodic scheduler tasks (Celery Beat)
- Async notification processing
"""

import os
import logging
from celery import Celery
from celery.schedules import crontab

logger = logging.getLogger("app_logger")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6380")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

# Use Redis DB 4 for Celery broker, DB 5 for results (avoid conflicts with other services)
_redis_auth = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
BROKER_URL = f"redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/4"
BACKEND_URL = f"redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/5"

celery_app = Celery(
    "notification_scheduler",
    broker=BROKER_URL,
    backend=BACKEND_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,       # 5 min hard limit
    task_soft_time_limit=240,  # 4 min soft limit
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,

    # Celery Beat schedule
    beat_schedule={
        "fire-scheduled-notifications": {
            "task": "app.notification_layer.celery_tasks.fire_scheduled_notifications",
            "schedule": 60.0,  # every 60 seconds
        },
        "check-job-deadlines": {
            "task": "app.notification_layer.celery_tasks.check_job_deadlines",
            "schedule": crontab(minute=0, hour="*/1"),  # every hour
        },
        "expire-banners": {
            "task": "app.notification_layer.celery_tasks.expire_banners",
            "schedule": 60.0,  # every 60 seconds
        },
        # AI chatbot — scheduled queries + anomaly subscriptions
        "ai-run-due-scheduled-queries": {
            "task": "app.ai_chat_layer.scheduler.run_due_scheduled_queries",
            "schedule": 60.0,
        },
        "ai-evaluate-anomaly-subs": {
            "task": "app.ai_chat_layer.scheduler.evaluate_anomaly_subs",
            "schedule": 120.0,
        },
        # Clawback WhatsApp notifications (app.whatsapp_layer.tasks).
        # Each task takes a Redis lock (wa:lock:<job>) shared with the
        # asyncio fallback in scheduler.py, so it runs once per interval.
        "wa-evaluate-clawback-reminders": {
            "task": "app.whatsapp_layer.tasks.evaluate_clawback_reminders",
            "schedule": 900.0,  # every 15 min
        },
        "wa-evaluate-clawback-digests": {
            "task": "app.whatsapp_layer.tasks.evaluate_clawback_digests",
            "schedule": 900.0,  # every 15 min
        },
        "wa-mark-missed-health-checks": {
            "task": "app.whatsapp_layer.tasks.mark_missed_health_checks",
            "schedule": crontab(minute=5),  # hourly
        },
        "wa-poll-whatsapp-batches": {
            "task": "app.whatsapp_layer.tasks.poll_whatsapp_batches",
            "schedule": 300.0,  # every 5 min (also sends due retries)
        },
        "wa-sync-whatsapp-templates": {
            "task": "app.whatsapp_layer.tasks.sync_whatsapp_templates",
            "schedule": 900.0,  # every 15 min
        },
        "wa-refresh-account-health": {
            "task": "app.whatsapp_layer.tasks.refresh_whatsapp_account_health",
            "schedule": crontab(minute=10),  # hourly
        },
        "wa-cleanup-webhook-events": {
            "task": "app.whatsapp_layer.tasks.cleanup_whatsapp_webhook_events",
            "schedule": crontab(minute=30, hour=2),  # daily, 02:30 UTC
        },
        "wa-failure-rate-alert": {
            "task": "app.whatsapp_layer.tasks.whatsapp_failure_rate_alert",
            "schedule": crontab(minute=15),  # hourly
        },
    },
)

# Auto-discover tasks
celery_app.autodiscover_tasks(["app.notification_layer", "app.ai_chat_layer", "app.whatsapp_layer"])
