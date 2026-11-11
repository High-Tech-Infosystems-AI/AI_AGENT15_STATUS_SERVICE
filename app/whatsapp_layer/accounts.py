"""Provider account helpers: credentials, webhook URL, health refresh,
serialisation. The access token is decrypted only to build
ProviderCredentials and is never logged or returned."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.exc import SQLAlchemyError
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


CHANNEL_TYPE = "whatsapp"
DEFAULT_PURPOSE = "default"


def live_accounts(db: Session):
    """Query over the accounts that still exist (not soft-deleted). Every
    lookup in this service starts here (channels contract §2.1)."""
    return db.query(WhatsAppProviderAccount).filter(WhatsAppProviderAccount.deleted_at.is_(None))


def is_usable(account: Optional[WhatsAppProviderAccount], organization_id: int) -> bool:
    """§2.5 test: belongs to the org, not deleted, status 'active'."""
    return (account is not None and account.deleted_at is None
            and account.status == "active"
            and int(account.organization_id) == int(organization_id))


def _usable_by_id(db: Session, organization_id: int,
                  account_id: Optional[int]) -> Optional[WhatsAppProviderAccount]:
    if account_id is None:
        return None
    account = (live_accounts(db)
               .filter(WhatsAppProviderAccount.id == int(account_id),
                       WhatsAppProviderAccount.organization_id == organization_id,
                       WhatsAppProviderAccount.status == "active")
               .first())
    return account if is_usable(account, organization_id) else None


def _default_account_id(db: Session, organization_id: int, purpose: str) -> Optional[int]:
    """The chn_channel_defaults row's account id for (org, purpose, whatsapp).
    The table belongs to the channel service; if it is not there yet the
    lookup degrades to "no default" instead of breaking sends."""
    from app.whatsapp_layer.models import ChannelDefault
    try:
        row = (db.query(ChannelDefault.account_id)
               .filter(ChannelDefault.organization_id == organization_id,
                       ChannelDefault.purpose == purpose,
                       ChannelDefault.channel_type == CHANNEL_TYPE)
               .first())
    except SQLAlchemyError as exc:
        logger.warning("chn_channel_defaults lookup failed (org %s, purpose %s): %s",
                       organization_id, purpose, exc)
        return None
    return int(row[0]) if row and row[0] is not None else None


def get_active_account(db: Session, organization_id: int, purpose: str = "clawback",
                       account_id: Optional[int] = None) -> Optional[WhatsAppProviderAccount]:
    """The account to send from (channels contract §2.5):

    1. `account_id` given: that account if usable, else None (no fallback).
    2. The org's default for `purpose`, if usable.
    3. The org's `default` purpose default, if usable.
    4. The org's lowest-id usable account.
    5. None (callers keep their skip / error behaviour).

    Usable = same org, not deleted, status 'active'.
    """
    if account_id is not None:
        return _usable_by_id(db, organization_id, account_id)
    purposes = [purpose] if purpose == DEFAULT_PURPOSE else [purpose, DEFAULT_PURPOSE]
    for p in purposes:
        account = _usable_by_id(db, organization_id,
                                _default_account_id(db, organization_id, p))
        if account is not None:
            return account
    return (live_accounts(db)
            .filter(WhatsAppProviderAccount.organization_id == organization_id,
                    WhatsAppProviderAccount.status == "active")
            .order_by(WhatsAppProviderAccount.id.asc())
            .first())


def get_any_account(db: Session, organization_id: int) -> Optional[WhatsAppProviderAccount]:
    """The org's lowest-id non-deleted account, whatever its status."""
    return (live_accounts(db)
            .filter(WhatsAppProviderAccount.organization_id == organization_id)
            .order_by(WhatsAppProviderAccount.id.asc())
            .first())


def resolve_or_any(db: Session, organization_id: int,
                   purpose: str = "clawback") -> Optional[WhatsAppProviderAccount]:
    """The account resolved for `purpose`; failing that, any non-deleted
    account of the org (e.g. one saved without a token yet), for owning
    drafts and showing connection status."""
    return (get_active_account(db, organization_id, purpose=purpose)
            or get_any_account(db, organization_id))


def get_org_account(db: Session, organization_id: int,
                    account_id: int) -> Optional[WhatsAppProviderAccount]:
    """A non-deleted account of this org (any status), else None."""
    return (live_accounts(db)
            .filter(WhatsAppProviderAccount.id == int(account_id),
                    WhatsAppProviderAccount.organization_id == organization_id)
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
                         f"until the token is updated in Settings → Channels."),
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
