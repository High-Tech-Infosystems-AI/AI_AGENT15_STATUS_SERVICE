"""Web Push (VAPID) sender + key management.

Responsibilities:
  - Resolve the active VAPID keypair (env > on-disk > auto-generate-and-persist).
  - Expose `vapid_public_key_b64url()` for the frontend to subscribe with.
  - `send_to_user(user_id, payload)`: fan out to every active subscription and
    auto-prune endpoints that come back 404/410 (browser unsubscribed).

Design notes:
  - We do best-effort delivery in-process. For high volume this should move to a
    Celery task; the call site is structured so swapping it for `.delay()` is a
    one-liner.
  - VAPID keys are written to `settings.VAPID_KEYS_FILE` on first generation so
    restarts don't invalidate every subscription. The file is text JSON,
    permissions are not enforced — keep it out of source control.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from typing import Iterable, Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
from sqlalchemy.orm import Session

from app.chat_layer.models import ChatPushSubscription
from app.core import settings

logger = logging.getLogger("app_logger")

_KEYS_LOCK = threading.Lock()
_CACHED_KEYS: Optional[dict] = None


# ---------- VAPID key management ----------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _generate_vapid_keypair() -> dict:
    """Generate a fresh prime256v1 keypair and return the VAPID-style encoding.

    `public_key` is the uncompressed point (65 bytes) base64url-encoded —
    that's the format the browser PushManager.subscribe expects in
    applicationServerKey. `private_key` is the raw 32-byte scalar
    base64url-encoded — that's the format pywebpush + the VAPID spec want.
    """
    private = ec.generate_private_key(ec.SECP256R1(), default_backend())
    private_numbers = private.private_numbers()
    private_bytes = private_numbers.private_value.to_bytes(32, "big")

    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "public_key": _b64url_encode(public_bytes),
        "private_key": _b64url_encode(private_bytes),
    }


def _load_or_create_keys() -> dict:
    """Resolution order:
      1. VAPID_PUBLIC_KEY + VAPID_PRIVATE_KEY env vars (preferred for prod).
      2. JSON file at settings.VAPID_KEYS_FILE (auto-managed for dev).
      3. Generate fresh, write to (2), return.
    """
    pub = (getattr(settings, "VAPID_PUBLIC_KEY", "") or "").strip()
    priv = (getattr(settings, "VAPID_PRIVATE_KEY", "") or "").strip()
    if pub and priv:
        return {"public_key": pub, "private_key": priv}

    keys_file = getattr(settings, "VAPID_KEYS_FILE", "") or "./vapid_keys.json"
    if os.path.isfile(keys_file):
        try:
            with open(keys_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("public_key") and data.get("private_key"):
                return data
        except Exception as exc:
            logger.warning("VAPID keys file unreadable (%s): %s — regenerating",
                           keys_file, exc)

    keys = _generate_vapid_keypair()
    try:
        os.makedirs(os.path.dirname(keys_file) or ".", exist_ok=True)
        with open(keys_file, "w", encoding="utf-8") as f:
            json.dump(keys, f)
        logger.warning(
            "Generated new VAPID keypair at %s. To make this stable across "
            "deploys, copy these into env as VAPID_PUBLIC_KEY / "
            "VAPID_PRIVATE_KEY.", keys_file,
        )
    except Exception as exc:
        logger.error("Could not persist VAPID keys to %s: %s — push will work "
                     "for this process only", keys_file, exc)
    return keys


def _keys() -> dict:
    global _CACHED_KEYS
    if _CACHED_KEYS is None:
        with _KEYS_LOCK:
            if _CACHED_KEYS is None:
                _CACHED_KEYS = _load_or_create_keys()
    return _CACHED_KEYS


def vapid_public_key_b64url() -> str:
    """Public key the frontend passes to PushManager.subscribe."""
    return _keys()["public_key"]


# ---------- Subscription helpers ----------

def hash_endpoint(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def upsert_subscription(db: Session, *, user_id: int, endpoint: str,
                        p256dh: str, auth: str,
                        user_agent: Optional[str]) -> ChatPushSubscription:
    """Insert a new subscription or refresh ownership of an existing one.

    A given endpoint is unique across the whole system, but a user may switch
    accounts in the same browser; if we see a different user re-subscribe with
    the same endpoint we transfer ownership rather than duplicating.
    """
    h = hash_endpoint(endpoint)
    existing = (db.query(ChatPushSubscription)
                  .filter(ChatPushSubscription.endpoint_hash == h)
                  .first())
    if existing:
        existing.user_id = user_id
        existing.p256dh = p256dh
        existing.auth_secret = auth
        existing.user_agent = user_agent
        db.commit()
        db.refresh(existing)
        return existing

    row = ChatPushSubscription(
        user_id=user_id, endpoint=endpoint, endpoint_hash=h,
        p256dh=p256dh, auth_secret=auth, user_agent=user_agent,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_subscription_by_endpoint(db: Session, endpoint: str) -> int:
    h = hash_endpoint(endpoint)
    deleted = (db.query(ChatPushSubscription)
                 .filter(ChatPushSubscription.endpoint_hash == h)
                 .delete(synchronize_session=False))
    db.commit()
    return int(deleted or 0)


def list_subscriptions_for_user(db: Session,
                                user_id: int) -> list[ChatPushSubscription]:
    return (db.query(ChatPushSubscription)
              .filter(ChatPushSubscription.user_id == user_id)
              .all())


# ---------- Send ----------

def _send_one(sub: ChatPushSubscription, payload: dict) -> tuple[bool, bool]:
    """Returns (sent_ok, gone). `gone=True` means the subscription is no
    longer valid and should be deleted by the caller."""
    sub_info = {
        "endpoint": sub.endpoint,
        "keys": {"p256dh": sub.p256dh, "auth": sub.auth_secret},
    }
    keys = _keys()
    vapid_claims = {"sub": getattr(settings, "VAPID_SUBJECT",
                                   "mailto:admin@hrmis.local")}
    try:
        webpush(
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=keys["private_key"],
            vapid_claims=vapid_claims,
            ttl=getattr(settings, "WEB_PUSH_TTL_SECONDS", 86400),
        )
        return True, False
    except WebPushException as e:
        status = getattr(e.response, "status_code", None) if e.response else None
        if status in (404, 410):
            logger.info("push subscription gone (status=%s) endpoint=%s — "
                        "will delete", status, sub.endpoint[:80])
            return False, True
        logger.warning("webpush failed status=%s endpoint=%s: %s",
                       status, sub.endpoint[:80], e)
        return False, False
    except Exception as e:
        logger.exception("webpush unexpected error endpoint=%s: %s",
                         sub.endpoint[:80], e)
        return False, False


def send_to_user(db: Session, user_id: int, payload: dict) -> dict:
    """Fan out `payload` to every push subscription registered for `user_id`.

    Returns a small stats dict so callers can log delivery counts. Expired
    subscriptions are pruned in the same transaction.
    """
    subs = list_subscriptions_for_user(db, user_id)
    sent = 0
    gone_ids: list[int] = []
    for sub in subs:
        ok, gone = _send_one(sub, payload)
        if ok:
            sent += 1
        if gone:
            gone_ids.append(sub.id)

    if gone_ids:
        (db.query(ChatPushSubscription)
           .filter(ChatPushSubscription.id.in_(gone_ids))
           .delete(synchronize_session=False))
        db.commit()

    return {"sent": sent, "pruned": len(gone_ids), "total_subscriptions": len(subs)}


def send_to_users(db: Session, user_ids: Iterable[int], payload: dict) -> dict:
    """Convenience wrapper over send_to_user for a list of recipients."""
    total = {"sent": 0, "pruned": 0, "total_subscriptions": 0}
    for uid in user_ids:
        try:
            stats = send_to_user(db, uid, payload)
            for k, v in stats.items():
                total[k] += v
        except Exception as e:
            logger.exception("push fan-out failed user=%s: %s", uid, e)
    return total
