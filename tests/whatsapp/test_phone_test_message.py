"""A test WhatsApp to any number (User Management's "Send test WhatsApp")."""
import pytest
from fastapi import HTTPException

from app.dependencies.tenant_auth import AuthCtx
from app.whatsapp_layer import crypto, seeds
from app.whatsapp_layer.api import provider_api
from app.whatsapp_layer.models import (
    WhatsAppMessageLog as L, WhatsAppProviderAccount as A, WhatsAppTemplate as T,
)
from app.whatsapp_layer.schemas import PhoneTest

ORG = 12


def _ctx(admin=True, perms=()):
    return AuthCtx(user_id=1, organization_id=ORG, role_id=1, role_name="r",
                   role_type="admin" if admin else "user", permissions=set(perms),
                   is_super_admin=False, is_admin=admin, token="t")


@pytest.fixture
def org(wa_db, monkeypatch):
    monkeypatch.setattr("app.whatsapp_layer.people.get_users", lambda _db, ids: {
        int(i): {"id": int(i), "name": f"User {i}", "phone_e164": None, "enabled": True}
        for i in ids if i})
    seeds.seed_defaults(wa_db, ORG, user_id=1)
    for tpl in wa_db.query(T).all():
        tpl.approval_status = "approved"
    wa_db.add(T(organization_id=ORG, name="hrmis_whatsapp_test", language="en_US",
                approval_status="approved", purpose="generic", body_text_named="Hello test."))
    wa_db.commit()
    return wa_db


def _account(db):
    db.add(A(organization_id=ORG, provider="omnichannel", display_name="HTI",
             base_url="https://api.broadnet.test", access_token_enc=crypto.encrypt("tok"),
             webhook_token="w" * 32, status="active"))
    db.commit()


def _send(db, ctx=None, **kw):
    body = PhoneTest(**{"phone_country_code": "+91", "phone_number": "9876543210", **kw})
    return provider_api.send_test_to_phone(body, None, ctx or _ctx(), db)


def test_sends_the_test_template_to_the_typed_number(org):
    _account(org)
    out = _send(org, user_id=7)
    assert out["status"] == "submitted" and out["template_name"] == "hrmis_whatsapp_test"
    log = org.query(L).one()
    assert (log.kind, log.phone_e164, log.recipient_user_id, log.recipient_name) == (
        "test", "+919876543210", 7, "User 7")


def test_a_chosen_template_and_provider_failure_come_back_in_the_log(org):
    _account(org)
    tpl = org.query(T).filter(T.name == "clawback_manager_note").one()
    out = _send(org, phone_number="9876500000", template_id=tpl.id)
    assert out["template_name"] == "clawback_manager_note"
    assert out["status"] == "failed" and out["error_message"] == "Mock: number failed"


def test_refusals(org):
    with pytest.raises(HTTPException) as err:
        _send(org)
    assert err.value.status_code == 409                     # no account connected
    _account(org)
    with pytest.raises(HTTPException) as err:
        _send(org, phone_number="12ab")
    assert err.value.status_code == 422
    with pytest.raises(HTTPException) as err:
        _send(org, ctx=_ctx(admin=False))
    assert err.value.status_code == 403
    assert _send(org, ctx=_ctx(admin=False, perms=[provider_api.PERM]))["status"] == "submitted"
