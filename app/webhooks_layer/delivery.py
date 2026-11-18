"""Turning events into signed webhook deliveries, sending them, and retrying.

An event creates one `pending` delivery per subscribed endpoint (fan_out).
Sending is started right after the event is accepted (deliver_now) and the
scheduler picks up anything due (run_due), so a delivery is never lost if
the first send can't happen. A delivery is claimed with a conditional UPDATE
before it is sent, so two runners never send the same one.

Each request carries:
  X-HRMIS-Event        event name, e.g. candidate_created
  X-HRMIS-Event-Id     stable id of the event (the same on retries/redeliveries)
  X-HRMIS-Timestamp    unix seconds when this attempt was signed
  X-HRMIS-Signature    sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" with the endpoint secret>
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from app.webhooks_layer import catalog
from app.webhooks_layer.models import WebhookDelivery as D, WebhookEndpoint as E
from app.webhooks_layer.urls import url_problem
from app.whatsapp_layer import crypto

logger = logging.getLogger("app_logger")

# Wait before attempt 2, 3, … ; six attempts span about 8.5 hours.
BACKOFF_SECONDS = [60, 300, 1800, 7200, 21600]
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
TIMEOUT_SECONDS = 10
# Failed attempts in a row before an endpoint is switched off.
AUTO_DISABLE_AFTER = 50
STALE_CLAIM = timedelta(minutes=10)
KEEP_RESPONSE_CHARS = 2000
RETENTION_DAYS = 30
USER_AGENT = "HRMIS-Webhooks/1.0"

_SENSITIVE_KEY = re.compile(r"pass|otp|secret|token|hash|api_?key|authori", re.IGNORECASE)

# (status_code or None, response text, error or None, duration_ms)
PostResult = Tuple[Optional[int], str, Optional[str], int]


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def redact(value: Any) -> Any:
    """Drop fields whose names look like credentials, at any depth."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if not _SENSITIVE_KEY.search(str(k))}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _org_id(data: Dict[str, Any]) -> Optional[int]:
    try:
        return int(data.get("organization_id"))
    except (TypeError, ValueError):
        return None


def build_payload(event_id: str, event_name: str, org_id: int, data: Dict[str, Any], now: datetime) -> str:
    return json.dumps({
        "id": event_id,
        "type": event_name,
        "created_at": now.replace(microsecond=0).isoformat() + "Z",
        "organization_id": org_id,
        "data": redact(data or {}),
    }, default=str, separators=(",", ":"))


def fan_out(db, event_name: str, data: Dict[str, Any], now: Optional[datetime] = None) -> List[int]:
    """Queue the event for every active endpoint of its organisation that subscribed to it."""
    if event_name not in catalog.SUBSCRIBABLE:
        return []
    org_id = _org_id(data or {})
    if org_id is None:
        return []
    endpoints = (db.query(E)
                 .filter(E.organization_id == org_id, E.is_active == 1, E.deleted_at.is_(None))
                 .all())
    targets = [e for e in endpoints if catalog.subscribed(e.events, event_name)]
    if not targets:
        return []
    now = now or datetime.utcnow()
    event_id = "evt_" + uuid.uuid4().hex
    payload = build_payload(event_id, event_name, org_id, data, now)
    rows = [D(endpoint_id=e.id, organization_id=org_id, event_id=event_id, event_name=event_name,
              payload=payload, status="pending", attempts=0, max_attempts=MAX_ATTEMPTS,
              next_attempt_at=now) for e in targets]
    db.add_all(rows)
    db.commit()
    return [r.id for r in rows]


def _claim(db, delivery_id: int, now: datetime) -> bool:
    claimed = (db.query(D)
               .filter(D.id == delivery_id, D.status.in_(("pending", "retrying")))
               .update({D.status: "sending", D.claimed_at: now}, synchronize_session=False))
    db.commit()
    return claimed == 1


def _post(url: str, body: bytes, headers: Dict[str, str]) -> PostResult:
    started = time.monotonic()
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                text = b""
                for chunk in resp.iter_bytes():
                    text += chunk
                    if len(text) >= KEEP_RESPONSE_CHARS:
                        break
                return (resp.status_code, text[:KEEP_RESPONSE_CHARS].decode("utf-8", "replace"), None,
                        int((time.monotonic() - started) * 1000))
    except httpx.TimeoutException:
        return None, "", f"No response within {TIMEOUT_SECONDS} seconds.", int((time.monotonic() - started) * 1000)
    except httpx.HTTPError as exc:
        return None, "", f"Could not connect: {type(exc).__name__}.", int((time.monotonic() - started) * 1000)


def attempt(db, delivery_id: int, now: Optional[datetime] = None,
            post: Callable[[str, bytes, Dict[str, str]], PostResult] = _post,
            count_failures: bool = True) -> Optional[D]:
    """Send one delivery once. Returns it, or None if another runner has it."""
    now = now or datetime.utcnow()
    if not _claim(db, delivery_id, now):
        return None
    d = db.get(D, delivery_id)
    db.refresh(d)  # the claim was a bulk UPDATE; reload so status changes are written
    endpoint = db.get(E, d.endpoint_id)
    d.claimed_at = None
    if endpoint is None or endpoint.deleted_at is not None or not endpoint.is_active:
        d.status, d.next_attempt_at = "failed", None
        d.error = "The endpoint is disabled or deleted."
        db.commit()
        return d

    problem = url_problem(endpoint.url)
    if problem:
        status, text, error, ms = None, "", problem, 0
    else:
        body = d.payload.encode("utf-8")
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-HRMIS-Event": d.event_name,
            "X-HRMIS-Event-Id": d.event_id,
            "X-HRMIS-Delivery": str(d.id),
            "X-HRMIS-Timestamp": str(timestamp),
            "X-HRMIS-Signature": sign(crypto.decrypt(endpoint.secret_enc), timestamp, body),
        }
        status, text, error, ms = post(endpoint.url, body, headers)

    d.attempts += 1
    d.response_status, d.response_body, d.duration_ms = status, (text or None), ms
    d.error = error if error else (None if status and 200 <= status < 300 else f"The endpoint answered {status}.")
    endpoint.last_delivery_at = now
    if status is not None and 200 <= status < 300:
        d.status, d.delivered_at, d.next_attempt_at = "succeeded", now, None
        endpoint.consecutive_failures, endpoint.last_success_at = 0, now
    else:
        if count_failures:
            endpoint.consecutive_failures = (endpoint.consecutive_failures or 0) + 1
            if endpoint.consecutive_failures >= AUTO_DISABLE_AFTER:
                endpoint.is_active = 0
                endpoint.disabled_reason = f"Switched off after {AUTO_DISABLE_AFTER} failed deliveries in a row."
                logger.warning("Webhook endpoint %s switched off after repeated failures", endpoint.id)
        if d.attempts < d.max_attempts:
            wait = BACKOFF_SECONDS[min(d.attempts - 1, len(BACKOFF_SECONDS) - 1)]
            d.status, d.next_attempt_at = "retrying", now + timedelta(seconds=wait)
        else:
            d.status, d.next_attempt_at = "failed", None
    db.commit()
    return d


def run_due(db, now: Optional[datetime] = None, limit: int = 100,
            post: Callable[[str, bytes, Dict[str, str]], PostResult] = _post) -> Dict[str, int]:
    """Send deliveries whose time has come; release claims left by a crashed runner."""
    now = now or datetime.utcnow()
    released = (db.query(D)
                .filter(D.status == "sending", D.claimed_at < now - STALE_CLAIM)
                .update({D.status: "retrying", D.next_attempt_at: now, D.claimed_at: None},
                        synchronize_session=False))
    db.commit()
    due = [row.id for row in (db.query(D.id)
                              .filter(D.status.in_(("pending", "retrying")), D.next_attempt_at <= now)
                              .order_by(D.next_attempt_at, D.id).limit(limit).all())]
    counts = {"released": released, "succeeded": 0, "retrying": 0, "failed": 0}
    for delivery_id in due:
        try:
            d = attempt(db, delivery_id, now=now, post=post)
        except Exception as exc:
            db.rollback()
            logger.error("Webhook delivery %s crashed: %s", delivery_id, exc, exc_info=True)
            continue
        if d is not None and d.status in counts:
            counts[d.status] += 1
    return counts


def deliver_now(delivery_ids: List[int]) -> None:
    """First attempt for freshly queued deliveries (runs after the event response)."""
    if not delivery_ids:
        return
    from app.database_Layer.db_config import SessionLocal
    db = SessionLocal()
    try:
        for delivery_id in delivery_ids:
            try:
                attempt(db, delivery_id)
            except Exception as exc:
                db.rollback()
                logger.error("Webhook delivery %s crashed: %s", delivery_id, exc, exc_info=True)
    finally:
        db.close()


def redeliver(db, delivery: D, now: Optional[datetime] = None) -> D:
    """A fresh delivery of the same event (same event id, so receivers can de-duplicate)."""
    now = now or datetime.utcnow()
    row = D(endpoint_id=delivery.endpoint_id, organization_id=delivery.organization_id,
            event_id=delivery.event_id, event_name=delivery.event_name, payload=delivery.payload,
            status="pending", attempts=0, max_attempts=MAX_ATTEMPTS, next_attempt_at=now)
    db.add(row)
    db.commit()
    return row


def ping(db, endpoint: E, now: Optional[datetime] = None,
         post: Callable[[str, bytes, Dict[str, str]], PostResult] = _post) -> D:
    """Send a test event once, right away. A failed test doesn't count towards switching the endpoint off."""
    now = now or datetime.utcnow()
    event_id = "evt_" + uuid.uuid4().hex
    row = D(endpoint_id=endpoint.id, organization_id=endpoint.organization_id, event_id=event_id,
            event_name=catalog.PING,
            payload=build_payload(event_id, catalog.PING, endpoint.organization_id,
                                  {"message": "Test event from HRMIS", "endpoint_id": endpoint.id}, now),
            status="pending", attempts=0, max_attempts=1, next_attempt_at=now)
    db.add(row)
    db.commit()
    return attempt(db, row.id, now=now, post=post, count_failures=False) or row


def cleanup(db, now: Optional[datetime] = None) -> int:
    cutoff = (now or datetime.utcnow()) - timedelta(days=RETENTION_DAYS)
    deleted = (db.query(D)
               .filter(D.created_at < cutoff, D.status.in_(("succeeded", "failed")))
               .delete(synchronize_session=False))
    db.commit()
    return deleted
