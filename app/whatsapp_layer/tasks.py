"""Celery tasks for clawback WhatsApp notifications + the asyncio fallback tick.

Both entry points (Celery Beat and app.notification_layer.scheduler's
asyncio loop) go through `_run_locked`, which takes a Redis lock named
`wa:lock:<job>` with a TTL just under the job's interval and does NOT
release it on completion. The lock therefore doubles as a throttle: the
job runs at most once per interval across every pod and both modes.

Message sends: in Celery mode each queued log becomes a
`send_whatsapp_message` task; in fallback mode they are sent inline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from sqlalchemy import func

from app.notification_layer import redis_manager
from app.notification_layer.celery_app import celery_app

logger = logging.getLogger("app_logger")

# job name → interval in seconds (keep in sync with celery_app.beat_schedule)
JOB_INTERVALS: Dict[str, int] = {
    "evaluate_clawback_reminders": 900,
    "evaluate_clawback_digests": 900,
    "mark_missed_health_checks": 3600,
    "poll_whatsapp_batches": 300,
    "sync_whatsapp_templates": 900,
    "refresh_whatsapp_account_health": 3600,
    "cleanup_whatsapp_webhook_events": 86400,
    "whatsapp_failure_rate_alert": 3600,
}
LOCK_MARGIN_SECONDS = 30
SEND_LOCK_TTL = 120
FAILURE_RATE_THRESHOLD = 0.10
FAILURE_RATE_MIN_SAMPLE = 3


def _session():
    from app.database_Layer.db_config import SessionLocal
    return SessionLocal()


def _run_locked(job: str, fn: Callable, *args) -> str:
    ttl = max(30, JOB_INTERVALS[job] - LOCK_MARGIN_SECONDS)
    if not redis_manager.acquire_scheduler_lock(f"wa:lock:{job}", ttl=ttl):
        logger.debug("Skipping %s — ran recently or another instance holds the lock", job)
        return "skipped"
    db = _session()
    try:
        result = fn(db, *args)
        logger.info("%s → %s", job, result)
        return str(result)
    except Exception as exc:
        db.rollback()
        logger.error("%s failed: %s", job, exc, exc_info=True)
        return f"error={exc}"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Job bodies (db, dispatch) — shared by Celery and the fallback loop
# ---------------------------------------------------------------------------
def _reminders(db, dispatch=None):
    from app.whatsapp_layer import clawback_engine
    return clawback_engine.run_reminders(db, dispatch=dispatch)


def _digests(db, dispatch=None):
    from app.whatsapp_layer import digest_engine
    return digest_engine.run_digests(db, dispatch=dispatch)


def _missed(db, *_):
    from app.whatsapp_layer import clawback_engine
    return {"missed": clawback_engine.mark_missed(db)}


def _poll(db, dispatch=None):
    from app.whatsapp_layer import sender
    return {"polled": sender.poll_batches(db), "dispatched": sender.dispatch_due(db, dispatch=dispatch)}


def _sync_templates(db, *_):
    from app.whatsapp_layer import templates_service
    return templates_service.sync_templates(db)


def _account_health(db, *_):
    from app.whatsapp_layer import accounts
    from app.whatsapp_layer.models import WhatsAppProviderAccount as A
    n = 0
    for acct in db.query(A).filter(A.status.in_(("active", "auth_error"))).all():
        try:
            accounts.refresh_health(db, acct)
            db.commit()
            n += 1
        except Exception as exc:
            db.rollback()
            logger.warning("Health refresh failed for WhatsApp account %s: %s", acct.id, exc)
    return {"refreshed": n}


def _cleanup_webhooks(db, *_):
    from app.whatsapp_layer import webhooks
    return {"deleted": webhooks.cleanup(db)}


def check_failure_alerts(db, now: Optional[datetime] = None) -> Dict[str, int]:
    """Hourly: alert an org's admins when >10% of last hour's sends failed,
    an account is in auth_error, or a template was rejected."""
    from app.whatsapp_layer import alerts
    from app.whatsapp_layer.models import (
        WhatsAppMessageLog as L, WhatsAppProviderAccount as A, WhatsAppTemplate as T,
    )
    now = now or datetime.utcnow()
    since = now - timedelta(hours=1)
    sent = 0
    rows = (db.query(L.organization_id, L.status, func.count(L.id))
            .filter(L.created_at >= since,
                    L.status.in_(("submitted", "sent", "delivered", "read", "failed")))
            .group_by(L.organization_id, L.status).all())
    per_org: Dict[int, Dict[str, int]] = {}
    for org_id, status, n in rows:
        d = per_org.setdefault(org_id, {"total": 0, "failed": 0})
        d["total"] += int(n)
        if status == "failed":
            d["failed"] += int(n)
    for org_id, d in per_org.items():
        if d["total"] >= FAILURE_RATE_MIN_SAMPLE and d["failed"] / d["total"] > FAILURE_RATE_THRESHOLD:
            if alerts.notify_org_admins(
                    db, org_id, title="WhatsApp messages are failing",
                    message=(f"{d['failed']} of {d['total']} WhatsApp messages failed in the last "
                             f"hour. Check Settings → WhatsApp logs."),
                    dedupe_key=f"failure_rate:{now:%Y%m%d%H}", event="alert_failure_rate"):
                sent += 1
    for acct in db.query(A).filter(A.status == "auth_error").all():
        if alerts.notify_org_admins(
                db, acct.organization_id, title="WhatsApp account needs attention",
                message=(f"The WhatsApp provider rejects the access token for "
                         f"'{acct.display_name}'. Update it in Settings → WhatsApp."),
                dedupe_key=f"auth_error:{acct.id}", event="alert_account_error"):
            sent += 1
    for tpl in db.query(T).filter(T.approval_status == "rejected", T.deleted_at.is_(None),
                                  T.updated_at >= now - timedelta(days=2)).all():
        if alerts.notify_org_admins(
                db, tpl.organization_id, title="WhatsApp template rejected",
                message=f"Template '{tpl.name}' was rejected: {tpl.rejection_reason or 'no reason given'}.",
                dedupe_key=f"template_rejected:{tpl.id}", event="alert_template_rejected"):
            sent += 1
    return {"alerts": sent}


def _failure_alerts(db, *_):
    return check_failure_alerts(db)


_JOBS: Dict[str, Callable] = {
    "evaluate_clawback_reminders": _reminders,
    "evaluate_clawback_digests": _digests,
    "mark_missed_health_checks": _missed,
    "poll_whatsapp_batches": _poll,
    "sync_whatsapp_templates": _sync_templates,
    "refresh_whatsapp_account_health": _account_health,
    "cleanup_whatsapp_webhook_events": _cleanup_webhooks,
    "whatsapp_failure_rate_alert": _failure_alerts,
}


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------
def _celery_dispatch(log_id: int) -> None:
    send_whatsapp_message.delay(log_id)


@celery_app.task(name="app.whatsapp_layer.tasks.send_whatsapp_message", bind=True)
def send_whatsapp_message(self, log_id: int):
    """Send one queued log. A short per-log lock stops two workers sending
    the same message."""
    from app.whatsapp_layer import sender
    lock = f"wa:send:{log_id}"
    if not redis_manager.acquire_scheduler_lock(lock, ttl=SEND_LOCK_TTL):
        return "skipped"
    db = _session()
    try:
        log = sender.send_log(db, int(log_id))
        return f"status={log.status if log else 'missing'}"
    except Exception as exc:
        db.rollback()
        logger.error("send_whatsapp_message %s failed: %s", log_id, exc, exc_info=True)
        return f"error={exc}"
    finally:
        db.close()
        redis_manager.release_scheduler_lock(lock)


@celery_app.task(name="app.whatsapp_layer.tasks.evaluate_clawback_reminders", bind=True)
def evaluate_clawback_reminders(self):
    return _run_locked("evaluate_clawback_reminders", _reminders, _celery_dispatch)


@celery_app.task(name="app.whatsapp_layer.tasks.evaluate_clawback_digests", bind=True)
def evaluate_clawback_digests(self):
    return _run_locked("evaluate_clawback_digests", _digests, _celery_dispatch)


@celery_app.task(name="app.whatsapp_layer.tasks.mark_missed_health_checks", bind=True)
def mark_missed_health_checks(self):
    return _run_locked("mark_missed_health_checks", _missed)


@celery_app.task(name="app.whatsapp_layer.tasks.poll_whatsapp_batches", bind=True)
def poll_whatsapp_batches(self):
    return _run_locked("poll_whatsapp_batches", _poll, _celery_dispatch)


@celery_app.task(name="app.whatsapp_layer.tasks.sync_whatsapp_templates", bind=True)
def sync_whatsapp_templates(self):
    return _run_locked("sync_whatsapp_templates", _sync_templates)


@celery_app.task(name="app.whatsapp_layer.tasks.refresh_whatsapp_account_health", bind=True)
def refresh_whatsapp_account_health(self):
    return _run_locked("refresh_whatsapp_account_health", _account_health)


@celery_app.task(name="app.whatsapp_layer.tasks.cleanup_whatsapp_webhook_events", bind=True)
def cleanup_whatsapp_webhook_events(self):
    return _run_locked("cleanup_whatsapp_webhook_events", _cleanup_webhooks)


@celery_app.task(name="app.whatsapp_layer.tasks.whatsapp_failure_rate_alert", bind=True)
def whatsapp_failure_rate_alert(self):
    return _run_locked("whatsapp_failure_rate_alert", _failure_alerts)


# ---------------------------------------------------------------------------
# Asyncio fallback (called every 60 s from notification_layer.scheduler,
# in a worker thread). Each job only runs when its lock is free.
# ---------------------------------------------------------------------------
def run_fallback_tick() -> None:
    for job, fn in _JOBS.items():
        try:
            _run_locked(job, fn)  # dispatch=None → sends inline
        except Exception as exc:
            logger.error("WhatsApp fallback job %s crashed: %s", job, exc, exc_info=True)
