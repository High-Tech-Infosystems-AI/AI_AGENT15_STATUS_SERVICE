"""Accounts can be saved without an access token so the webhook URL exists
before the provider issues credentials; the first token activates them."""
import pytest
from fastapi import HTTPException

from app.dependencies.tenant_auth import AuthCtx
from app.whatsapp_layer.api import provider_api
from app.whatsapp_layer.models import WhatsAppProviderAccount as A
from app.whatsapp_layer.schemas import AccountCreate, AccountUpdate

ORG = 12


def _ctx():
    return AuthCtx(user_id=1, organization_id=ORG, role_id=1, role_name="admin", role_type="admin",
                   permissions=set(), is_super_admin=False, is_admin=True, token="t")


def _create(db, **kw):
    body = AccountCreate(display_name="HTI WhatsApp", base_url="https://api.broadnet.test/", **kw)
    return provider_api.create_account(body, None, _ctx(), db)


def test_create_without_token_gives_webhook_url_and_stays_disabled(wa_db):
    acct = _create(wa_db)
    assert acct["status"] == "disabled"
    assert acct["token_last4"] is None
    assert "/status/whatsapp/webhooks/omnichannel/" in acct["webhook_url"]
    row = wa_db.query(A).one()
    assert row.access_token_enc is None and row.webhook_token


def test_blank_token_counts_as_missing(wa_db):
    assert _create(wa_db, access_token="   ")["status"] == "disabled"


def test_create_with_token_is_active(wa_db):
    acct = _create(wa_db, access_token="secret-token-1234")
    assert acct["status"] == "active" and acct["token_last4"] == "1234"


def test_first_token_activates_and_keeps_webhook_url(wa_db):
    acct = _create(wa_db)
    updated = provider_api.update_account(acct["id"], AccountUpdate(access_token="tok-abcd"),
                                          _ctx(), wa_db)
    assert updated["status"] == "active"
    assert updated["token_last4"] == "abcd"
    assert updated["webhook_url"] == acct["webhook_url"]


def test_first_token_respects_explicit_status(wa_db):
    acct = _create(wa_db)
    updated = provider_api.update_account(
        acct["id"], AccountUpdate(access_token="tok-abcd", status="disabled"), _ctx(), wa_db)
    assert updated["status"] == "disabled"


def test_later_token_does_not_reenable_disabled_account(wa_db):
    acct = _create(wa_db, access_token="tok-1111")
    provider_api.update_account(acct["id"], AccountUpdate(status="disabled"), _ctx(), wa_db)
    updated = provider_api.update_account(acct["id"], AccountUpdate(access_token="tok-2222"),
                                          _ctx(), wa_db)
    assert updated["status"] == "disabled"


def test_health_needs_a_token(wa_db):
    acct = _create(wa_db)
    with pytest.raises(HTTPException) as exc:
        provider_api.refresh_account_health(acct["id"], _ctx(), wa_db)
    assert exc.value.status_code == 422
