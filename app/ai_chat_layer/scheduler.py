"""Celery tasks for AI chatbot scheduling.

Two beat tasks (registered in `notification_layer/celery_app.py`):

  - `run_due_scheduled_queries` (60s) — scans `ai_scheduled_query` for rows
    where `is_active = 1 AND next_run_at <= NOW()`, runs the agent under
    each user's identity, posts the answer into their AI thread, and
    materializes the next `next_run_at`.

  - `evaluate_anomaly_subs` (120s) — scans `ai_anomaly_subscription`,
    evaluates each metric, and fires an AI message + push when the
    threshold is breached and the cooldown has elapsed.

Anomalies use built-in metric evaluators (no LLM call) so they're cheap
to scan; only the *narrative* of a triggered anomaly uses Gemini.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import text

from app.ai_chat_layer import agent as ai_agent
from app.ai_chat_layer.api.dm import get_or_create_ai_dm
from app.ai_chat_layer.api.schedules_api import _next_run_at
from app.ai_chat_layer.models import (
    AiAnomalySubscription, AiScheduledQuery,
)
from app.database_Layer.db_config import SessionLocal
from app.notification_layer.celery_app import celery_app
from app.notification_layer.redis_manager import (
    acquire_scheduler_lock, release_scheduler_lock,
)

logger = logging.getLogger("app_logger")

LOCK_KEY_SCHED = "ai:schedule:lock"
LOCK_KEY_ANOMALY = "ai:anomaly:lock"


def _user_for(db, user_id: int) -> Optional[Dict[str, Any]]:
    """Build the same `user` dict the auth layer would. Used to drive the
    agent under the schedule owner's identity (no real auth token here).
    """
    row = db.execute(
        text("SELECT u.id, u.username, u.name, u.role_id, r.name AS role_name "
             "FROM users u LEFT JOIN roles r ON r.id = u.role_id "
             "WHERE u.id = :uid LIMIT 1"),
        {"uid": user_id},
    ).first()
    if not row:
        return None
    m = row._mapping
    return {
        "user_id": m["id"],
        "role_id": m["role_id"],
        "role_name": m["role_name"],
        "username": m["username"],
        "name": m["name"],
    }


@celery_app.task(name="app.ai_chat_layer.scheduler.run_due_scheduled_queries")
def run_due_scheduled_queries() -> int:
    """Fire any scheduled queries whose `next_run_at <= now`."""
    if not acquire_scheduler_lock(LOCK_KEY_SCHED, ttl=55):
        return 0
    fired = 0
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        due = (db.query(AiScheduledQuery)
               .filter(AiScheduledQuery.is_active == 1)
               .filter(AiScheduledQuery.next_run_at != None)  # noqa: E711
               .filter(AiScheduledQuery.next_run_at <= now)
               .limit(50)
               .all())
        for sched in due:
            user = _user_for(db, sched.user_id)
            if not user:
                continue
            try:
                conv_id, _bot = get_or_create_ai_dm(db, sched.user_id)
                ai_agent.run_turn(
                    db=db, user=user,
                    prompt=sched.prompt,
                    refs=sched.refs or [],
                    conversation_id=conv_id,
                    ip_address=None,
                )
                sched.last_run_at = now
                sched.next_run_at = _next_run_at(sched.cron_expr, base=now)
                db.commit()
                fired += 1
            except Exception:
                logger.exception("scheduled query %s failed", sched.id)
                db.rollback()
        return fired
    finally:
        db.close()
        release_scheduler_lock(LOCK_KEY_SCHED)


# ----- anomaly evaluators -----

def _evaluate_stuck_candidates(db, user, params: Dict[str, Any]) -> Optional[str]:
    """Find candidates whose CURRENT pipeline stage was set > N days ago.

    Per-(candidate, job) stage lives in `candidate_pipeline_status` —
    `latest = 1` is the active row, `created_at` is when that stage was
    entered, `pipeline_stage_id` joins to `pipeline_stages.name`. So
    "days in current stage" = NOW() − cps.created_at on the latest row.
    """
    threshold_days = int(params.get("threshold_days", 10))
    cutoff = datetime.utcnow() - timedelta(days=threshold_days)
    rows = db.execute(
        text("""
        SELECT cj.candidate_id,
               cj.job_id,
               ps.name AS stage,
               TIMESTAMPDIFF(DAY, cps.created_at, NOW()) AS days_in_stage,
               j.title AS job_title
          FROM candidate_pipeline_status cps
          JOIN candidate_jobs cj ON cj.id = cps.candidate_job_id
          JOIN job_openings   j  ON j.id  = cj.job_id
          JOIN user_jobs_assigned uja ON uja.job_id = cj.job_id
          JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
         WHERE uja.user_id = :uid
           AND cps.latest = 1
           AND cps.created_at <= :cutoff
         ORDER BY cps.created_at ASC
         LIMIT 25
        """),
        {"uid": user["user_id"], "cutoff": cutoff},
    ).all()
    if not rows:
        return None
    sample = rows[0]._mapping
    return (
        f"You have {len(rows)} candidate(s) stuck in the same stage for more "
        f"than {threshold_days} days. Example: candidate {sample['candidate_id']} "
        f"in stage '{sample['stage']}' on '{sample['job_title']}' "
        f"({sample['days_in_stage']} days)."
    )


def _evaluate_no_activity(db, user, params: Dict[str, Any]) -> Optional[str]:
    days = int(params.get("threshold_days", 14))
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        text("""
        SELECT j.id, j.title
          FROM job_openings j
          JOIN user_jobs_assigned uja ON uja.job_id = j.id
         WHERE uja.user_id = :uid
           AND UPPER(j.status) = 'ACTIVE'
           AND NOT EXISTS (
               SELECT 1 FROM candidate_jobs cj
                WHERE cj.job_id = j.id AND cj.applied_at >= :cutoff
           )
         LIMIT 10
        """),
        {"uid": user["user_id"], "cutoff": cutoff},
    ).all()
    if not rows:
        return None
    titles = ", ".join(r._mapping.get("title") or f"#{r._mapping.get('id')}"
                       for r in rows[:3])
    return (f"{len(rows)} active job(s) had no candidate activity in the "
            f"last {days} days (e.g. {titles}).")


_EVALUATORS = {
    "stuck_candidates": _evaluate_stuck_candidates,
    "no_activity": _evaluate_no_activity,
}


@celery_app.task(name="app.ai_chat_layer.scheduler.evaluate_anomaly_subs")
def evaluate_anomaly_subs() -> int:
    if not acquire_scheduler_lock(LOCK_KEY_ANOMALY, ttl=110):
        return 0
    fired = 0
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        active = (db.query(AiAnomalySubscription)
                  .filter(AiAnomalySubscription.is_active == 1)
                  .all())
        for sub in active:
            if sub.last_fired_at and (now - sub.last_fired_at) < timedelta(minutes=sub.cooldown_min):
                continue
            evaluator = _EVALUATORS.get(sub.metric_key)
            if not evaluator:
                continue
            user = _user_for(db, sub.user_id)
            if not user:
                continue
            try:
                summary = evaluator(db, user, sub.params or {})
            except Exception:
                logger.exception("anomaly evaluator %s failed", sub.metric_key)
                continue
            if not summary:
                continue
            # Hand the summary to the agent so it composes a proper reply
            # (refs, suggestions). The model is not what detected the
            # anomaly — it just narrates and offers next steps.
            try:
                conv_id, _ = get_or_create_ai_dm(db, sub.user_id)
                ai_agent.run_turn(
                    db=db, user=user,
                    prompt=("[Anomaly alert] " + summary +
                            " — explain the impact and suggest 2-3 next "
                            "steps, citing the affected entities."),
                    refs=[], conversation_id=conv_id, ip_address=None,
                )
                sub.last_fired_at = now
                db.commit()
                fired += 1
            except Exception:
                logger.exception("anomaly fire failed sub=%s", sub.id)
                db.rollback()
        return fired
    finally:
        db.close()
        release_scheduler_lock(LOCK_KEY_ANOMALY)
