"""The provider picks the account from the access token, so the saved
username has to be the user the token was issued for."""
import base64
import json

import pytest
from fastapi import HTTPException

from app.whatsapp_layer import crypto
from app.whatsapp_layer.api import provider_api
from app.whatsapp_layer.schemas import AccountCreate, AccountUpdate
from tests.whatsapp.test_account_without_token import _create, _ctx


def _jwt(sub):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{claims}.sig"


def test_token_subject():
    assert crypto.token_subject(_jwt("testUserB")) == "testUserB"
    assert crypto.token_subject("plain-token") is None
    assert crypto.token_subject("a.!!!.c") is None
    assert crypto.token_subject(None) is None


def test_create_refuses_a_token_of_another_user(wa_db):
    with pytest.raises(HTTPException) as exc:
        _create(wa_db, username="hightechmo_omni", access_token=_jwt("testUserB"))
    assert exc.value.status_code == 422
    assert "issued for 'testUserB'" in str(exc.value.detail)


def test_create_takes_the_username_from_the_token(wa_db):
    assert _create(wa_db, access_token=_jwt("testUserB"))["username"] == "testUserB"


def test_changing_only_the_username_is_checked_against_the_saved_token(wa_db):
    acct = _create(wa_db, username="testUserB", access_token=_jwt("testUserB"))
    with pytest.raises(HTTPException) as exc:
        provider_api.update_account(acct["id"], AccountUpdate(username="hightechmo_omni"), _ctx(), wa_db)
    assert exc.value.status_code == 422


def test_new_token_with_matching_username_is_saved(wa_db):
    acct = _create(wa_db, username="testUserB", access_token=_jwt("testUserB"))
    updated = provider_api.update_account(
        acct["id"], AccountUpdate(username="hightechmo_omni", access_token=_jwt("hightechmo_omni")),
        _ctx(), wa_db)
    assert updated["username"] == "hightechmo_omni"
    # A new token alone brings its user along.
    updated = provider_api.update_account(acct["id"], AccountUpdate(access_token=_jwt("other")),
                                          _ctx(), wa_db)
    assert updated["username"] == "other"


def test_plain_tokens_keep_any_username(wa_db):
    acct = _create(wa_db, username="htiuser", access_token="plain-token-1234")
    updated = provider_api.update_account(acct["id"], AccountUpdate(username="renamed"), _ctx(), wa_db)
    assert updated["username"] == "renamed"
