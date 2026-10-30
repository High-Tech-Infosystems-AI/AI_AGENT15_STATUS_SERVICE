"""Message logs and the send path shared by reminders, digests, send-now,
retry and test sends.

Lifecycle: queued → submitted (provider accepted; batchId stored) → sent →
delivered → read, or failed / skipped. Every skip is logged with a reason.

Retries: 5xx / timeout / network errors are retried 3 times with a backoff of
1, 5 and 15 minutes (`next_attempt_at`; picked up by the 5-minute poll).
4xx and "number failed" are final. 401 marks the account auth_error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.whatsapp_layer.models import (
    WhatsAppMessageLog as L, WhatsAppOptOut, WhatsAppProviderAccount as A,
    WhatsAppTemplate as T,
)

logger = logging.getLogger("app_logger")

RETRY_BACKOFF_MINUTES = [1, 5, 15]
STATUS_ORDER = {"queued": 0, "submitted": 1, "sent": 2, "delivered": 3, "read": 4}
SUMMARY_STATUSES = ["queued", "submitted", "sent", "delivered", "read", "failed", "skipped"]


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def rule_template(db: Session, template_id: Optional[int]) -> Optional[T]:
    if not template_id:
        return None
    return db.query(T).filter(T.id == template_id, T.deleted_at.is_(None)).first()


def approved_template_for_purpose(db: Session, organization_id: int, purpose: str) -> Optional[T]:
    """Latest approved, active template with this purpose (falls back to any
    template of the purpose so the skip log can still name it)."""
    base = db.query(T).filter(T.organization_id == organization_id, T.purpose == purpose,
                              T.deleted_at.is_(None), T.is_active == 1)
    tpl = (base.filter(T.approval_status == "approved")
           .order_by(T.version.desc(), T.id.desc()).first())
    return tpl or base.order_by(T.version.desc(), T.id.desc()).first()


def is_opted_out(db: Session, organization_id: int, phone_e164: Optional[str]) -> bool:
    if not phone_e164:
        return False
    return db.query(WhatsAppOptOut.id).filter(
        WhatsAppOptOut.organization_id == organization_id,
        WhatsAppOptOut.phone_e164 == phone_e164).first() is not None


# ---------------------------------------------------------------------------
# Creating logs
# ---------------------------------------------------------------------------
def create_log(db: Session, **fields) -> Tuple[L, bool]:
    """Insert-or-ignore on UNIQUE(idempotency_key). Returns (log, created)."""
    key = fields["idempotency_key"]
    existing = db.query(L).filter(L.idempotency_key == key).first()
    if existing:
        return existing, False
    fields.setdefault("status", "queued")
    fields.setdefault("attempts", 0)
    # Explicit UTC stamp: the dispatch / poll windows compare against utcnow().
    fields.setdefault("created_at", datetime.utcnow())
    log = L(**fields)
    try:
        with db.begin_nested():
            db.add(log)
            db.flush()
    except IntegrityError:
        return db.query(L).filter(L.idempotency_key == key).first(), False
    log.correlation_id = f"cbk_{log.id}"
    return log, True


def mark_skipped(log: L, reason: str) -> None:
    log.status = "skipped"
    log.skip_reason = reason


def skip_reason_for(*, settings, account, template, user: Optional[Dict],
                    opted_out: bool, check_org_enabled: bool = False) -> Optional[str]:
    """First applicable skip reason (contract §2.8), or None to send."""
    if check_org_enabled and (settings is None or not settings.enabled):
        return "disabled_org"
    if account is None or account.status != "active":
        return "account_inactive"
    if template is None or template.approval_status != "approved" or not template.is_active \
            or template.deleted_at is not None:
        return "template_not_approved"
    if not user or not user.get("enabled", True):
        return "recipient_disabled"
    if not user.get("phone_e164"):
        return "no_phone"
    if opted_out:
        return "opted_out"
    return None


def apply_prechecks(db: Session, log: L, *, settings, account, template, user: Optional[Dict],
                    check_org_enabled: bool = False) -> None:
    """Render the body and decide queued vs skipped for a fresh log."""
    from app.whatsapp_layer import templates_service as ts
    if template is not None:
        log.template_id = template.id
        log.template_name = template.name
        log.rendered_body = ts.render_body(template, log.params or {})
    reason = skip_reason_for(
        settings=settings, account=account, template=template, user=user,
        opted_out=is_opted_out(db, log.organization_id, (user or {}).get("phone_e164")),
        check_org_enabled=check_org_enabled)
    if reason:
        mark_skipped(log, reason)
    else:
        log.status = "queued"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _fail(log: L, code: Optional[str], message: Optional[str], now: datetime) -> None:
    log.status = "failed"
    log.error_code = (code or "")[:32] or None
    log.error_message = message
    log.failed_at = now
    log.next_attempt_at = None


def send_log(db: Session, log_id: int, now: Optional[datetime] = None) -> Optional[L]:
    """Send one queued log through the org's provider and record the result."""
    from app.whatsapp_layer import accounts as acct_svc, templates_service as ts
    from app.whatsapp_layer.providers.base import TemplateRef
    from app.whatsapp_layer.providers.registry import get_provider

    now = now or datetime.utcnow()
    log = db.query(L).filter(L.id == log_id).first()
    if log is None or log.status != "queued":
        return log

    account = db.query(A).filter(A.id == log.account_id).first() if log.account_id else None
    if account is None or account.status != "active":
        account = acct_svc.get_active_account(db, log.organization_id)
    if account is None:
        mark_skipped(log, "account_inactive")
        db.commit()
        return log
    template = rule_template(db, log.template_id)
    if template is None or template.approval_status != "approved" or not template.is_active:
        mark_skipped(log, "template_not_approved")
        db.commit()
        return log
    if not log.phone_e164:
        mark_skipped(log, "no_phone")
        db.commit()
        return log
    if is_opted_out(db, log.organization_id, log.phone_e164):
        mark_skipped(log, "opted_out")
        db.commit()
        return log

    log.account_id, log.provider = account.id, account.provider
    try:
        creds = acct_svc.credentials(account)
    except Exception as exc:
        log.attempts = (log.attempts or 0) + 1
        _fail(log, "credentials", f"Stored token could not be decrypted: {exc}", now)
        db.commit()
        return log

    header_vars, body_vars = ts.send_variables(template.variable_map, log.params or {})
    correlation = log.correlation_id or f"cbk_{log.id}"
    try:
        result = get_provider(account.provider).send_template(
            creds, log.phone_e164.lstrip("+"),
            TemplateRef(template.name, template.language,
                        ts.body_param_names(template)),
            header_vars, body_vars, correlation)
    except Exception as exc:  # defensive: providers should return SendResult
        logger.error("WhatsApp send crashed for log %s: %s", log.id, exc, exc_info=True)
        from app.whatsapp_layer.providers.base import SendResult
        result = SendResult(accepted=False, error_code="exception",
                            error_message=str(exc)[:500], retryable=True)

    log.attempts = (log.attempts or 0) + 1
    log.correlation_id = correlation
    if result.accepted:
        log.status = "submitted"
        log.provider_ref = result.provider_ref
        log.provider_message_id = result.provider_message_id or log.provider_message_id
        log.submitted_at = now
        log.error_code = log.error_message = None
        log.next_attempt_at = None
    elif result.duplicate:
        mark_skipped(log, "provider_duplicate")
        log.provider_ref = result.provider_ref
    elif result.auth_error:
        _fail(log, result.error_code or "401", result.error_message, now)
        acct_svc.mark_auth_error(db, account, result.error_message or "Unauthorized")
    elif result.retryable and log.attempts <= len(RETRY_BACKOFF_MINUTES):
        log.status = "queued"
        log.error_code = (result.error_code or "")[:32] or None
        log.error_message = result.error_message
        log.next_attempt_at = now + timedelta(minutes=RETRY_BACKOFF_MINUTES[log.attempts - 1])
    else:
        _fail(log, result.error_code, result.error_message, now)
    db.commit()
    logger.info("WhatsApp log %s (%s) → %s attempt=%s", log.id, log.kind, log.status, log.attempts)
    return log


def dispatch_many(db: Session, log_ids: Iterable[int],
                  dispatch: Optional[Callable[[int], None]] = None) -> None:
    """Hand queued logs to the sender: a Celery enqueue in worker mode, an
    inline send otherwise (asyncio fallback / API)."""
    for log_id in log_ids:
        try:
            if dispatch is not None:
                dispatch(log_id)
            else:
                send_log(db, log_id)
        except Exception as exc:
            db.rollback()
            logger.error("Dispatch of WhatsApp log %s failed: %s", log_id, exc, exc_info=True)


def dispatch_due(db: Session, now: Optional[datetime] = None,
                 dispatch: Optional[Callable[[int], None]] = None, limit: int = 500) -> int:
    """Send queued logs that are due: retries whose backoff has elapsed, and
    fresh logs older than 2 minutes that no worker picked up (crash safety).
    Logs older than 24 h are left alone (not back-filled)."""
    now = now or datetime.utcnow()
    rows = (db.query(L.id, L.next_attempt_at, L.created_at)
            .filter(L.status == "queued", L.created_at >= now - timedelta(hours=24))
            .order_by(L.id.asc()).limit(limit).all())
    due = [r[0] for r in rows
           if (r[1] is not None and r[1] <= now)
           or (r[1] is None and r[2] is not None and r[2] <= now - timedelta(minutes=2))]
    dispatch_many(db, due, dispatch)
    return len(due)


def poll_batches(db: Session, now: Optional[datetime] = None, limit: int = 500) -> int:
    """`submitted` logs older than 2 minutes: ask the provider for the batch
    status. `Done` with processed ≥ 1 → sent."""
    from app.whatsapp_layer import accounts as acct_svc
    from app.whatsapp_layer.providers.base import ProviderError
    from app.whatsapp_layer.providers.registry import get_provider

    now = now or datetime.utcnow()
    logs = (db.query(L).filter(L.status == "submitted", L.provider_ref.isnot(None),
                               L.submitted_at <= now - timedelta(minutes=2),
                               L.submitted_at >= now - timedelta(days=3))
            .order_by(L.id.asc()).limit(limit).all())
    updated = 0
    accounts: Dict[int, Optional[A]] = {}
    for log in logs:
        if log.account_id not in accounts:
            accounts[log.account_id] = db.query(A).filter(A.id == log.account_id).first()
        account = accounts[log.account_id]
        if account is None or account.status != "active":
            continue
        try:
            st = get_provider(account.provider).get_send_status(
                acct_svc.credentials(account), log.provider_ref)
        except ProviderError as exc:
            if exc.auth_error:
                acct_svc.mark_auth_error(db, account, exc.message)
            logger.warning("Batch status poll failed for log %s: %s", log.id, exc.message)
            continue
        except Exception as exc:
            logger.warning("Batch status poll failed for log %s: %s", log.id, exc)
            continue
        if st.status == "sent":
            log.status, log.sent_at = "sent", log.sent_at or now
            updated += 1
        elif st.status == "failed":
            _fail(log, "batch_failed", st.error_message or "Batch failed", now)
            updated += 1
    db.commit()
    return updated


def advance_status(log: L, new_status: str, at: datetime,
                   error_code: Optional[str] = None, error_message: Optional[str] = None) -> bool:
    """Apply a delivery status without ever moving backwards. Returns True
    when the log changed."""
    if new_status == "failed":
        if log.status in ("delivered", "read", "failed", "skipped"):
            return False
        _fail(log, error_code or "provider_failed", error_message, at)
        return True
    if new_status not in STATUS_ORDER or log.status not in STATUS_ORDER:
        return False
    if STATUS_ORDER[new_status] <= STATUS_ORDER[log.status]:
        return False
    log.status = new_status
    if new_status in ("sent", "delivered", "read") and not log.sent_at:
        log.sent_at = at
    if new_status in ("delivered", "read") and not log.delivered_at:
        log.delivered_at = at
    if new_status == "read":
        log.read_at = at
    return True


# ---------------------------------------------------------------------------
# Serialisation (contract §2.8 Log)
# ---------------------------------------------------------------------------
def _iso(dt) -> Optional[str]:
    return dt.replace(microsecond=0).isoformat() + "Z" if isinstance(dt, datetime) else None


def serialize_log(log: L) -> Dict[str, Any]:
    return {
        "id": log.id, "kind": log.kind, "status": log.status, "skip_reason": log.skip_reason,
        "recipient_user_id": log.recipient_user_id, "recipient_name": log.recipient_name,
        "recipient_type": log.recipient_type, "phone_e164": log.phone_e164,
        "template_id": log.template_id, "template_name": log.template_name,
        "rule_id": log.rule_id, "rule_name": log.rule_name, "digest_rule_id": log.digest_rule_id,
        "candidate_job_id": log.candidate_job_id,
        "candidate_job_status_id": log.candidate_job_status_id,
        "candidate_id": log.candidate_id, "candidate_name": log.candidate_name,
        "job_id": log.job_id, "job_title": log.job_title, "day_number": log.day_number,
        "scheduled_for": _iso(log.scheduled_for), "rendered_body": log.rendered_body,
        "params": log.params or {}, "digest_snapshot": log.digest_snapshot,
        "provider": log.provider, "provider_ref": log.provider_ref,
        "provider_message_id": log.provider_message_id,
        "error_code": log.error_code, "error_message": log.error_message,
        "attempts": log.attempts or 0,
        "submitted_at": _iso(log.submitted_at), "sent_at": _iso(log.sent_at),
        "delivered_at": _iso(log.delivered_at), "read_at": _iso(log.read_at),
        "failed_at": _iso(log.failed_at), "triggered_by": log.triggered_by,
        "created_at": _iso(log.created_at),
    }
