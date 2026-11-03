"""Provider account helpers: credentials, webhook URL, health refresh,
serialisation. The access token is decrypted only to build
ProviderCredentials and is never logged or returned."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.whatsapp_layer import crypto
from app.whatsapp_layer.models import WhatsAppProviderAccount
from app.whatsapp_layer.providers.base import ProviderCredentials

logger = logging.getLogger("app_logger")

WEBHOOK_PATH = "/status/whatsapp/webhooks/{provider}/{token}"


def new_webhook_token() -> str:
    return secrets.token_urlsafe(32)[:48]


def webhook_url(account: WhatsAppProviderAccount) -> str:
    from app.core import settings
    base = (getattr(settings, "PUBLIC_API_BASE_URL", "") or "").strip().rstrip("/")
    if base and "://" not in base:
        base = f"https://{base}"
    return base + WEBHOOK_PATH.format(provider=account.provider, token=account.webhook_token)


def credentials(account: WhatsAppProviderAccount) -> ProviderCredentials:
    token = crypto.decrypt(account.access_token_enc) if account.access_token_enc else ""
    return ProviderCredentials(
        base_url=account.base_url,
        access_token=token,
        username=account.username,
        waba_id=account.waba_id,
        phone_number_id=account.phone_number_id,
    )


def get_active_account(db: Session, organization_id: int) -> Optional[WhatsAppProviderAccount]:
    """The org's sending account (one active account per org)."""
    return (db.query(WhatsAppProviderAccount)
            .filter(WhatsAppProviderAccount.organization_id == organization_id,
                    WhatsAppProviderAccount.status == "active")
            .order_by(WhatsAppProviderAccount.id.asc())
            .first())


def get_any_account(db: Session, organization_id: int) -> Optional[WhatsAppProviderAccount]:
    return (db.query(WhatsAppProviderAccount)
            .filter(WhatsAppProviderAccount.organization_id == organization_id)
            .order_by(WhatsAppProviderAccount.id.asc())
            .first())


def mark_auth_error(db: Session, account: WhatsAppProviderAccount, message: str) -> None:
    """401 from the provider: stop sending from this account and raise an
    in-app alert to the org admins (deduplicated)."""
    if account.status != "auth_error":
        account.status = "auth_error"
        account.last_error = (message or "")[:2000]
        # Commit before alerting: the alert path may roll back on failure and
        # must not undo the status change.
        db.commit()
        logger.warning("WhatsApp account %s (org %s) marked auth_error",
                       account.id, account.organization_id)
        try:
            from app.whatsapp_layer import alerts
            alerts.notify_org_admins(
                db, account.organization_id,
                title="WhatsApp account needs attention",
                message=(f"The WhatsApp provider rejected the access token for "
                         f"'{account.display_name}'. Clawback reminders are paused "
                         f"until the token is updated in Settings → WhatsApp."),
                dedupe_key=f"auth_error:{account.id}",
                event="alert_account_error",
            )
        except Exception as exc:
            logger.warning("auth_error alert failed for account %s: %s", account.id, exc)


def refresh_health(db: Session, account: WhatsAppProviderAccount) -> WhatsAppProviderAccount:
    """Pull status / quality / limit from the provider and store it."""
    from app.whatsapp_layer.providers.registry import get_provider
    try:
        health = get_provider(account.provider).health_check(credentials(account))
    except Exception as exc:
        account.last_error = f"Health check failed: {exc}"[:2000]
        account.last_health_at = datetime.utcnow()
        db.flush()
        return account
    account.last_health_at = datetime.utcnow()
    if health.auth_error:
        mark_auth_error(db, account, health.error or "Unauthorized")
        return account
    if not health.ok and health.error:
        account.last_error = health.error[:2000]
        db.flush()
        return account
    account.quality_rating = health.quality_rating or account.quality_rating
    account.messaging_limit = health.messaging_limit or account.messaging_limit
    if health.sandbox is not None:
        account.sandbox = 1 if health.sandbox else 0
    account.display_phone_number = health.display_phone_number or account.display_phone_number
    account.waba_id = health.waba_id or account.waba_id
    account.phone_number_id = health.phone_number_id or account.phone_number_id
    account.last_error = None
    # A successful authenticated call clears a previous auth_error.
    if account.status == "auth_error":
        account.status = "active"
    db.flush()
    return account


def _iso(dt) -> Optional[str]:
    return dt.replace(microsecond=0).isoformat() + "Z" if isinstance(dt, datetime) else None


def serialize(account: WhatsAppProviderAccount) -> Dict[str, Any]:
    return {
        "id": account.id,
        "organization_id": account.organization_id,
        "provider": account.provider,
        "display_name": account.display_name,
        "base_url": account.base_url,
        "username": account.username,
        "token_last4": account.token_last4,
        "waba_id": account.waba_id,
        "phone_number_id": account.phone_number_id,
        "display_phone_number": account.display_phone_number,
        "webhook_url": webhook_url(account),
        "status": account.status,
        "quality_rating": account.quality_rating,
        "messaging_limit": account.messaging_limit,
        "sandbox": bool(account.sandbox),
        "last_health_at": _iso(account.last_health_at),
        "created_at": _iso(account.created_at),
        "updated_at": _iso(account.updated_at),
    }
