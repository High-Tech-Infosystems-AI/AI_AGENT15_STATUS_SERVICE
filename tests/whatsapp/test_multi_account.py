"""Several WhatsApp accounts per organisation (channels contract §2.1, §2.5,
§5.1): account resolution, clawback sends through the clawback default,
webhook routing per account, soft-deleted accounts, legacy routes."""
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.dependencies.tenant_auth import AuthCtx
from app.whatsapp_layer import accounts as acct_svc
from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import crypto, sender, seeds, webhooks
from app.whatsapp_layer.api import provider_api, templates_api, webhook_api
from app.whatsapp_layer.models import (
    ChannelDefault, WhatsAppMessageLog as L, WhatsAppProviderAccount as A,
    WhatsAppTemplate as T, WhatsAppWebhookEvent,
)
from app.whatsapp_layer.providers import registry
from app.whatsapp_layer.schemas import AccountCreate, AccountUpdate, TemplateWrite

ORG = 12
OTHER_ORG = 99
TODAY = date(2026, 10, 26)                                      # Monday
NOW = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)         # 10:30 IST


def _ctx(org=ORG):
    return AuthCtx(user_id=1, organization_id=org, role_id=1, role_name="admin", role_type="admin",
                   permissions=set(), is_super_admin=False, is_admin=True, token="t")


def _acct(db, name, org=ORG, status="active", deleted=False, token=None):
    a = A(organization_id=org, provider="omnichannel", display_name=name,
          base_url=f"https://{name.lower()}.broadnet.test", access_token_enc=crypto.encrypt("tok"),
          token_last4="tok", webhook_token=token or f"wh-{name.lower()}-" + "x" * 24,
          status="disabled" if deleted else status,
          deleted_at=datetime.utcnow() if deleted else None)
    db.add(a)
    db.flush()
    return a


def _default(db, purpose, account, org=ORG, channel_type="whatsapp"):
    db.add(ChannelDefault(organization_id=org, purpose=purpose, channel_type=channel_type,
                          account_id=account.id))
    db.flush()


# --- Resolution (§2.5) --------------------------------------------------------

def test_no_accounts_resolves_to_none(wa_db):
    assert acct_svc.get_active_account(wa_db, ORG) is None
    assert acct_svc.get_any_account(wa_db, ORG) is None


def test_lowest_usable_id_when_no_defaults(wa_db):
    _acct(wa_db, "Gone", deleted=True)
    _acct(wa_db, "Off", status="disabled")
    _acct(wa_db, "Broken", status="auth_error")
    _acct(wa_db, "Elsewhere", org=OTHER_ORG)
    first = _acct(wa_db, "First")
    _acct(wa_db, "Second")
    assert acct_svc.get_active_account(wa_db, ORG).id == first.id


def test_purpose_default_wins_over_org_default_and_lowest_id(wa_db):
    a1, a2, a3 = _acct(wa_db, "One"), _acct(wa_db, "Two"), _acct(wa_db, "Three")
    _default(wa_db, "default", a2)
    assert acct_svc.get_active_account(wa_db, ORG).id == a2.id              # org default
    _default(wa_db, "clawback", a3)
    assert acct_svc.get_active_account(wa_db, ORG).id == a3.id              # clawback default
    assert acct_svc.get_active_account(wa_db, ORG, purpose="clawback").id == a3.id
    assert acct_svc.get_active_account(wa_db, ORG, purpose="campaigns").id == a2.id
    assert acct_svc.get_active_account(wa_db, ORG, purpose="default").id == a2.id
    # Email defaults and other orgs' defaults are ignored.
    _default(wa_db, "workflows", a1, channel_type="email")
    assert acct_svc.get_active_account(wa_db, ORG, purpose="workflows").id == a2.id


def test_unusable_defaults_fall_through(wa_db):
    lowest = _acct(wa_db, "Lowest")
    off = _acct(wa_db, "Off", status="disabled")
    gone = _acct(wa_db, "Gone", deleted=True)
    foreign = _acct(wa_db, "Foreign", org=OTHER_ORG)
    _default(wa_db, "clawback", off)
    _default(wa_db, "default", gone)
    assert acct_svc.get_active_account(wa_db, ORG).id == lowest.id
    wa_db.query(ChannelDefault).delete()
    _default(wa_db, "clawback", foreign)                    # row points at another org
    assert acct_svc.get_active_account(wa_db, ORG).id == lowest.id
    _default(wa_db, "default", off)
    assert acct_svc.get_active_account(wa_db, ORG).id == lowest.id
    lowest.status = "auth_error"
    wa_db.flush()
    assert acct_svc.get_active_account(wa_db, ORG) is None


def test_explicit_account_id_never_falls_back(wa_db):
    a1, a2 = _acct(wa_db, "One"), _acct(wa_db, "Two")
    off = _acct(wa_db, "Off", status="disabled")
    gone = _acct(wa_db, "Gone", deleted=True)
    foreign = _acct(wa_db, "Foreign", org=OTHER_ORG)
    _default(wa_db, "clawback", a1)
    assert acct_svc.get_active_account(wa_db, ORG, account_id=a2.id).id == a2.id
    for bad in (off.id, gone.id, foreign.id, 12345):
        assert acct_svc.get_active_account(wa_db, ORG, account_id=bad) is None


def test_get_any_account_skips_deleted(wa_db):
    _acct(wa_db, "Gone", deleted=True)
    off = _acct(wa_db, "Off", status="disabled")
    assert acct_svc.get_any_account(wa_db, ORG).id == off.id
    assert acct_svc.resolve_or_any(wa_db, ORG).id == off.id


# --- Clawback sends go through the clawback default ---------------------------

def _item(cj, rec, joined_days_ago=7):
    joined = TODAY - timedelta(days=joined_days_ago)
    return {"candidate_job_id": cj, "candidate_job_status_id": cj + 1000, "status": "in_cooling",
            "organization_id": ORG, "candidate_id": f"C{cj}", "candidate_name": f"Cand {cj}",
            "job_id": 9, "job_title": "Sales Executive", "company_name": "Acme",
            "offer_accepted_at": None, "joined_at": joined.isoformat(), "cooling_period_days": 90,
            "clawback_completion_date": (joined + timedelta(days=90)).isoformat(),
            "cooling_period_closed": None, "recruiter_user_id": rec, "assigned_to": rec,
            "created_by": 3}


@pytest.fixture
def two_accounts(wa_db, monkeypatch):
    db = wa_db
    monkeypatch.setattr("app.whatsapp_layer.people.get_users", lambda _db, ids: {
        int(i): {"id": int(i), "name": f"User {i}", "email": f"u{i}@x", "organization_id": ORG,
                 "phone_e164": "+919876543210", "enabled": True} for i in ids if i})
    monkeypatch.setattr("app.whatsapp_layer.alerts.notify_org_admins", lambda *a, **k: None)
    st = E.get_settings(db, ORG)
    st.enabled = 1
    st.app_base_url = "https://hrmis.example.test"
    first, second = _acct(db, "Recruitment"), _acct(db, "Clawback")
    seeds.seed_defaults(db, ORG, user_id=1)
    for tpl in db.query(T).all():
        tpl.approval_status = "draft" if tpl.name.endswith(("_prompt", "_names")) else "approved"
    _default(db, "default", first)
    _default(db, "clawback", second)
    db.commit()

    sent = []
    mock = registry.get_provider("omnichannel")
    original = type(mock).send_template

    def spy(self, creds, *args, **kw):
        sent.append(creds.base_url)
        return original(self, creds, *args, **kw)
    monkeypatch.setattr(type(mock), "send_template", spy)
    return db, first, second, sent


def test_clawback_reminders_go_through_the_clawback_default(two_accounts):
    db, first, second, sent = two_accounts
    stats = E.evaluate_org_reminders(db, ORG, [_item(1, 7), _item(2, 8)], NOW)
    assert stats["queued"] == 2
    logs = db.query(L).all()
    assert {(l.status, l.account_id) for l in logs} == {("submitted", second.id)}
    assert sent == [second.base_url, second.base_url]


def test_send_records_the_account_actually_used(two_accounts):
    db, first, second, sent = two_accounts
    tpl = db.query(T).filter(T.approval_status == "approved").first()
    # Queued against the first account, which is then deleted: the send falls
    # back to the clawback account and the log records it.
    log = L(organization_id=ORG, kind="manual", idempotency_key="k-moved", status="queued",
            account_id=first.id, provider="omnichannel", template_id=tpl.id,
            template_name=tpl.name, phone_e164="+919876543210", params={})
    db.add(log)
    first.deleted_at, first.status = datetime.utcnow(), "disabled"
    db.commit()
    sender.send_log(db, log.id)
    assert (log.status, log.account_id) == ("submitted", second.id)
    assert sent == [second.base_url]


def test_send_keeps_the_queued_account_while_it_is_usable(two_accounts):
    db, first, second, sent = two_accounts
    tpl = db.query(T).filter(T.approval_status == "approved").first()
    log = L(organization_id=ORG, kind="manual", idempotency_key="k-keep", status="queued",
            account_id=first.id, provider="omnichannel", template_id=tpl.id,
            template_name=tpl.name, phone_e164="+919876543210", params={})
    db.add(log)
    db.commit()
    sender.send_log(db, log.id)
    assert (log.status, log.account_id) == ("submitted", first.id)
    assert sent == [first.base_url]


def test_send_skips_when_every_account_is_deleted(two_accounts):
    db, first, second, sent = two_accounts
    for a in (first, second):
        a.deleted_at, a.status = datetime.utcnow(), "disabled"
    db.commit()
    E.evaluate_org_reminders(db, ORG, [_item(1, 7)], NOW)
    assert db.query(L).one().skip_reason == "account_inactive"
    assert sent == []


# --- Webhooks per account -----------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    """Threads-safe SQLite + the webhook router (TestClient runs the app in
    another thread)."""
    from app.database_Layer.db_config import Base, get_db
    from app.whatsapp_layer import models as m
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine, tables=[
        mdl.__table__ for mdl in (m.WhatsAppProviderAccount, m.WhatsAppTemplate,
                                  m.WhatsAppMessageLog, m.WhatsAppWebhookEvent,
                                  m.WhatsAppOptOut, m.ChannelDefault)])
    db = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    monkeypatch.setattr("app.whatsapp_layer.campaign_forward.forward", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(webhook_api.router, prefix="/status/whatsapp")
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield db, TestClient(app)
    finally:
        db.close()
        engine.dispose()


def _status_payload(wamid):
    return {"entry": [{"changes": [{"field": "messages", "value": {"statuses": [
        {"id": wamid, "status": "delivered", "timestamp": str(int(datetime.utcnow().timestamp())),
         "recipient_id": "919876543210"}]}}]}]}


def test_webhooks_for_both_accounts_route_to_their_account(web):
    db, client = web
    a1, a2 = _acct(db, "One", token="t1" + "a" * 30), _acct(db, "Two", token="t2" + "b" * 30)
    now = datetime.utcnow()
    for i, (acct, wamid) in enumerate(((a1, "wamid.ONE"), (a2, "wamid.TWO"))):
        db.add(L(organization_id=ORG, kind="reminder", idempotency_key=f"k{i}", status="submitted",
                 account_id=acct.id, provider_message_id=wamid, phone_e164="+919876543210",
                 submitted_at=now, created_at=now))
    db.commit()

    for acct, wamid in ((a1, "wamid.ONE"), (a2, "wamid.TWO")):
        r = client.post(f"/status/whatsapp/webhooks/omnichannel/{acct.webhook_token}",
                        json=_status_payload(wamid))
        assert r.status_code == 200 and r.json() == {"received": True}
    events = db.query(WhatsAppWebhookEvent).order_by(WhatsAppWebhookEvent.id).all()
    assert [e.account_id for e in events] == [a1.id, a2.id]
    assert {l.provider_message_id: l.status for l in db.query(L).all()} == {
        "wamid.ONE": "delivered", "wamid.TWO": "delivered"}

    # Token in a header / HMAC signature pick the right account too.
    body = json.dumps(_status_payload("wamid.NONE")).encode()
    r = client.post("/status/whatsapp/webhooks/omnichannel",
                    headers={"Authorization": f"Bearer {a2.webhook_token}"}, content=body)
    assert r.status_code == 200
    sig = hmac.new(a1.webhook_token.encode(), body, hashlib.sha256).hexdigest()
    r = client.post("/status/whatsapp/webhooks/omnichannel",
                    headers={"X-Hub-Signature-256": f"sha256={sig}"}, content=body)
    assert r.status_code == 200
    events = db.query(WhatsAppWebhookEvent).order_by(WhatsAppWebhookEvent.id).all()
    assert [e.account_id for e in events[2:]] == [a2.id, a1.id]


def test_phone_fallback_only_matches_the_receiving_accounts_logs(wa_db):
    a1, a2 = _acct(wa_db, "One"), _acct(wa_db, "Two")
    now = datetime.utcnow()
    mine = L(organization_id=ORG, kind="reminder", idempotency_key="m", status="submitted",
             account_id=a1.id, phone_e164="+919876543210",
             submitted_at=now - timedelta(minutes=10), created_at=now)
    theirs = L(organization_id=ORG, kind="reminder", idempotency_key="t", status="submitted",
               account_id=a2.id, phone_e164="+919876543210", submitted_at=now, created_at=now)
    wa_db.add_all([mine, theirs])
    wa_db.commit()
    payload = _status_payload("wamid.UNKNOWN")
    events = registry.get_provider("omnichannel").parse_webhook({}, payload)
    webhooks.apply_events(wa_db, a1, events)
    assert (mine.status, theirs.status) == ("delivered", "submitted")


def test_deleted_accounts_webhooks_are_rejected(web):
    db, client = web
    gone = _acct(db, "Gone", deleted=True, token="tg" + "c" * 30)
    db.commit()
    assert webhooks.find_account(db, "omnichannel", gone.webhook_token) is None
    r = client.post(f"/status/whatsapp/webhooks/omnichannel/{gone.webhook_token}",
                    json=_status_payload("x"))
    assert r.status_code == 404
    r = client.get("/status/whatsapp/webhooks/omnichannel",
                   params={"hub.verify_token": gone.webhook_token, "hub.challenge": "42"})
    assert r.status_code == 404
    body = json.dumps(_status_payload("x")).encode()
    sig = hmac.new(gone.webhook_token.encode(), body, hashlib.sha256).hexdigest()
    r = client.post("/status/whatsapp/webhooks/omnichannel",
                    headers={"X-Hub-Signature-256": f"sha256={sig}",
                             "Authorization": f"Bearer {gone.webhook_token}"}, content=body)
    assert r.status_code == 404
    ev = db.query(WhatsAppWebhookEvent).one()
    assert ev.error == "unverified" and ev.account_id is None


# --- Background jobs and template sync skip deleted accounts -----------------

def test_health_task_and_template_sync_skip_deleted(wa_db, monkeypatch):
    from app.whatsapp_layer import tasks, templates_service
    live, gone = _acct(wa_db, "Live"), _acct(wa_db, "Gone", deleted=True)
    gone.status = "active"          # even if a deleted row was left active
    wa_db.commit()
    seen = []
    monkeypatch.setattr(acct_svc, "refresh_health", lambda _db, a: seen.append(("health", a.id)))
    monkeypatch.setattr(templates_service, "import_from_provider",
                        lambda _db, a: seen.append(("import", a.id)) or {"imported": 0, "updated": 0})
    tasks._account_health(wa_db)
    templates_service.sync_templates(wa_db, ORG)
    assert seen == [("health", live.id), ("import", live.id)]


def test_template_import_is_per_account(wa_db, monkeypatch):
    from app.whatsapp_layer import templates_service
    from app.whatsapp_layer.providers.base import ProviderTemplate
    a1, a2 = _acct(wa_db, "One"), _acct(wa_db, "Two")
    wa_db.add(T(organization_id=ORG, account_id=a1.id, name="hello", language="en_US",
                body_text_named="Hi", approval_status="approved"))
    wa_db.commit()
    monkeypatch.setattr(type(registry.get_provider("omnichannel")), "list_templates",
                        lambda self, creds: [ProviderTemplate(
                            name="hello", language="en_US", category="UTILITY", status="pending",
                            body_text="Hi", provider_template_id="p2")])
    templates_service.import_from_provider(wa_db, a2)
    wa_db.commit()
    rows = {t.account_id: t.approval_status for t in wa_db.query(T).filter(T.name == "hello")}
    assert rows == {a1.id: "approved", a2.id: "pending"}


# --- Legacy account routes ------------------------------------------------------

def _create(db, name="HTI WhatsApp", **kw):
    body = AccountCreate(display_name=name, base_url="https://api.broadnet.test/", **kw)
    return provider_api.create_account(body, None, _ctx(), db)


def test_legacy_post_creates_a_second_account(wa_db):
    first = _create(wa_db, "Recruitment", access_token="tok-1111")
    second = _create(wa_db, "Clawback", access_token="tok-2222")
    assert first["id"] != second["id"] and second["status"] == "active"
    assert first["webhook_url"] != second["webhook_url"]
    listed = provider_api.list_accounts(None, _ctx(), wa_db)["data"]
    assert [a["id"] for a in listed] == [first["id"], second["id"]]


def test_legacy_routes_hide_deleted_accounts(wa_db):
    acct = _create(wa_db, "Old", access_token="tok-1111")
    row = wa_db.query(A).get(acct["id"])
    row.deleted_at, row.status = datetime.utcnow(), "disabled"
    wa_db.commit()
    assert provider_api.list_accounts(None, _ctx(), wa_db)["data"] == []
    with pytest.raises(HTTPException) as err:
        provider_api.update_account(acct["id"], AccountUpdate(status="active"), _ctx(), wa_db)
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        provider_api.rotate_webhook_token(acct["id"], _ctx(), wa_db)
    assert err.value.status_code == 404


# --- Templates per account --------------------------------------------------------

def _tpl_body(name, **kw):
    return TemplateWrite(name=name, body_text="Hello {{recruiter_name}}, thanks.", **kw)


def test_template_create_and_list_per_account(wa_db):
    a1, a2 = _acct(wa_db, "One"), _acct(wa_db, "Two")
    foreign = _acct(wa_db, "Foreign", org=OTHER_ORG)
    gone = _acct(wa_db, "Gone", deleted=True)
    _default(wa_db, "clawback", a2)
    wa_db.add(T(organization_id=ORG, name="unassigned_draft", language="en_US",
                body_text_named="x", approval_status="draft"))
    wa_db.commit()

    default = templates_api.create_template(_tpl_body("for_clawback"), None, None, _ctx(), wa_db)
    assert default["account_id"] == a2.id
    by_query = templates_api.create_template(_tpl_body("by_query"), None, a1.id, _ctx(), wa_db)
    assert by_query["account_id"] == a1.id
    by_body = templates_api.create_template(_tpl_body("by_body", account_id=a1.id), None, None,
                                            _ctx(), wa_db)
    assert by_body["account_id"] == a1.id
    # The same name may exist once per account.
    same_name = templates_api.create_template(_tpl_body("for_clawback"), None, a1.id, _ctx(), wa_db)
    assert same_name["account_id"] == a1.id
    for bad in (foreign.id, gone.id, 12345):
        with pytest.raises(HTTPException) as err:
            templates_api.create_template(_tpl_body("nope"), None, bad, _ctx(), wa_db)
        assert err.value.status_code == 404

    def names(account_id=None):
        out = templates_api.list_templates(None, None, None, 1, 100, None, account_id,
                                           _ctx(), wa_db)
        return sorted((t["name"], t["account_id"]) for t in out["data"])
    assert names() == [("for_clawback", a2.id), ("unassigned_draft", None)]
    assert names(a1.id) == [("by_body", a1.id), ("by_query", a1.id), ("for_clawback", a1.id),
                            ("unassigned_draft", None)]
    with pytest.raises(HTTPException) as err:
        names(foreign.id)
    assert err.value.status_code == 404


def test_template_submit_uses_the_templates_own_account(wa_db, monkeypatch):
    from app.whatsapp_layer import templates_service
    a1, a2 = _acct(wa_db, "One"), _acct(wa_db, "Two")
    _default(wa_db, "clawback", a2)
    wa_db.commit()
    tpl = templates_api.create_template(_tpl_body("mine", sample_values={"recruiter_name": "A"}),
                                        None, a1.id, _ctx(), wa_db)
    used = []
    monkeypatch.setattr(templates_service, "submit",
                        lambda _db, t, account, uid: used.append(account.id))
    templates_api.submit_template(tpl["id"], _ctx(), wa_db)
    assert used == [a1.id]
    a1.status = "disabled"
    wa_db.commit()
    with pytest.raises(HTTPException) as err:
        templates_api.submit_template(tpl["id"], _ctx(), wa_db)
    assert err.value.status_code == 409
