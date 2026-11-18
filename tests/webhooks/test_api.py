"""/status/webhooks: permissions, org scoping, validation and one-time secrets."""
import pytest
from fastapi import BackgroundTasks, HTTPException

from app.dependencies.tenant_auth import AuthCtx
from app.webhooks_layer import api
from app.webhooks_layer.models import WebhookDelivery as D, WebhookEndpoint as E
from app.webhooks_layer.schemas import EndpointCreate, EndpointUpdate
from app.whatsapp_layer import crypto

ORG = 12


def _ctx(admin=True, perms=(), org=ORG):
    return AuthCtx(user_id=1, organization_id=org, role_id=1, role_name="r",
                   role_type="admin" if admin else "user", permissions=set(perms),
                   is_super_admin=False, is_admin=admin, token="t")


def _create(db, ctx=None, **kw):
    body = EndpointCreate(**{"name": "CRM", "url": "https://hooks.example.test/in",
                             "events": ["candidate_created"], **kw})
    return api.create_endpoint(body, None, ctx or _ctx(), db)


def test_create_returns_the_secret_once_and_stores_it_encrypted(wh_db):
    out = _create(wh_db)
    assert out["secret"].startswith("whsec_") and out["secret_last4"] == out["secret"][-4:]
    row = wh_db.get(E, out["id"])
    assert crypto.decrypt(row.secret_enc) == out["secret"] and row.organization_id == ORG
    listed = api.list_endpoints(None, _ctx(), wh_db)
    assert [e["id"] for e in listed] == [out["id"]] and "secret" not in listed[0]


def test_only_admins_or_developer_permission_can_manage(wh_db):
    with pytest.raises(HTTPException) as err:
        _create(wh_db, ctx=_ctx(admin=False))
    assert err.value.status_code == 403
    assert _create(wh_db, ctx=_ctx(admin=False, perms={"developer.manage"}))["id"]


@pytest.mark.parametrize("url, message", [
    ("http://hooks.example.test/in", "https://"),
    ("https://internal.example.test/in", "private network"),
    ("https://localhost/in", "private network"),
    ("https://user:pw@hooks.example.test/in", "user name and password"),
])
def test_unsafe_urls_are_refused(wh_db, url, message):
    with pytest.raises(HTTPException) as err:
        _create(wh_db, url=url)
    assert err.value.status_code == 422 and message in err.value.detail[0]["message"]


def test_unknown_events_are_refused_and_star_means_all(wh_db):
    with pytest.raises(HTTPException) as err:
        _create(wh_db, events=["candidate_created", "user_login_success"])
    assert "user_login_success" in err.value.detail[0]["message"]
    assert _create(wh_db, events=["*", "job_created"])["events"] == ["*"]


def test_another_orgs_endpoint_is_not_found(wh_db):
    out = _create(wh_db)
    with pytest.raises(HTTPException) as err:
        api.get_endpoint(out["id"], _ctx(org=99), wh_db)
    assert err.value.status_code in (403, 404)


def test_switching_back_on_clears_the_failure_state(wh_db):
    out = _create(wh_db)
    row = wh_db.get(E, out["id"])
    row.is_active, row.consecutive_failures, row.disabled_reason = 0, 50, "Switched off"
    wh_db.commit()
    updated = api.update_endpoint(out["id"], EndpointUpdate(is_active=True), _ctx(), wh_db)
    assert updated["is_active"] and updated["consecutive_failures"] == 0 and updated["disabled_reason"] is None


def test_rotate_changes_the_secret(wh_db):
    out = _create(wh_db)
    rotated = api.rotate_secret(out["id"], _ctx(), wh_db)
    assert rotated["secret"] != out["secret"]
    assert crypto.decrypt(wh_db.get(E, out["id"]).secret_enc) == rotated["secret"]


def test_deleted_endpoints_disappear_but_keep_their_log(wh_db):
    out = _create(wh_db)
    wh_db.add(D(endpoint_id=out["id"], organization_id=ORG, event_id="evt", event_name="candidate_created",
                payload="{}", status="succeeded"))
    wh_db.commit()
    api.delete_endpoint(out["id"], _ctx(), wh_db)
    assert api.list_endpoints(None, _ctx(), wh_db) == []
    assert api.list_deliveries(out["id"], None, None, 1, 25, _ctx(), wh_db)["total"] == 1


def test_redeliver_needs_an_active_endpoint(wh_db):
    out = _create(wh_db)
    d = D(endpoint_id=out["id"], organization_id=ORG, event_id="evt", event_name="candidate_created",
          payload='{"id":"evt"}', status="failed")
    wh_db.add(d)
    wh_db.commit()
    again = api.redeliver(d.id, BackgroundTasks(), _ctx(), wh_db)
    assert again["status"] == "pending" and again["event_id"] == "evt"
    api.update_endpoint(out["id"], EndpointUpdate(is_active=False), _ctx(), wh_db)
    with pytest.raises(HTTPException) as err:
        api.redeliver(d.id, BackgroundTasks(), _ctx(), wh_db)
    assert err.value.status_code == 409


def test_event_catalogue_lists_groups(wh_db):
    out = api.list_events(_ctx())
    names = {e["name"] for g in out["groups"] for e in g["events"]}
    assert "candidate_created" in names and "user_login_success" not in names
