"""The "Show list" flow and Today's check-ins (contract §6.7)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import crypto, seeds, today as TL, webhooks
from app.whatsapp_layer import digest_engine as D
from app.whatsapp_layer.api import webhook_api
from app.whatsapp_layer.models import (
    ClawbackDigestRule as DR, WhatsAppMessageLog as L, WhatsAppProviderAccount as A,
    WhatsAppTemplate as T,
)
from app.whatsapp_layer.providers.base import SendResult
from app.whatsapp_layer.providers.omnichannel import OmnichannelProvider

ORG = 12
TODAY = date(2026, 10, 26)
NOW = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)
PHONES = {7: "+919876543210", 8: "+919811112222", 50: "+919800000050"}
NAMES = {7: "Asha Rao", 8: "Vikram Das", 50: "Meera Iyer"}


def _item(cj, rec, name, ago=7):
    joined = TODAY - timedelta(days=ago)
    return {"candidate_job_id": cj, "candidate_job_status_id": cj + 1000, "status": "in_cooling",
            "organization_id": ORG, "candidate_id": f"C{cj}", "candidate_name": name, "job_id": 9,
            "job_title": "Sales Executive", "company_name": "Acme", "offer_accepted_at": None,
            "joined_at": joined.isoformat(), "cooling_period_days": 90,
            "clawback_completion_date": (joined + timedelta(days=90)).isoformat(),
            "cooling_period_closed": None, "recruiter_user_id": rec, "assigned_to": rec, "created_by": 3}


class FakeProvider(OmnichannelProvider):
    def __init__(self):
        self.texts = []

    def send_text(self, creds, to_digits, text, correlation_id):
        self.texts.append((to_digits, text))
        return SendResult(accepted=True, provider_ref="b1")


@pytest.fixture
def org(wa_db, monkeypatch):
    db = wa_db
    users = {i: {"id": i, "name": NAMES[i], "email": "x", "organization_id": ORG,
                 "phone_e164": PHONES[i], "enabled": True} for i in PHONES}
    monkeypatch.setattr("app.whatsapp_layer.people.get_users",
                        lambda _d, ids: {int(i): users[int(i)] for i in ids if int(i) in users})
    monkeypatch.setattr("app.whatsapp_layer.people.find_by_phone", lambda _d, _o, phone: next(
        (u for u in users.values() if u["phone_e164"].lstrip("+") == phone), None))
    monkeypatch.setattr("app.whatsapp_layer.people.admin_users", lambda _d, _o: [])
    monkeypatch.setattr("app.whatsapp_layer.clients.fetch_active_clawbacks", lambda *_a, **_k: {
        "items": [_item(1, 7, "Ravi Kumar"), _item(2, 7, "Priya Shah"), _item(3, 8, "Imran Ali")]})
    monkeypatch.setattr("app.whatsapp_layer.clients.subordinates_of", lambda _o, uid: {
        "subordinate_user_ids": [7, 8] if uid == 50 else []})
    monkeypatch.setattr(E, "local_now", lambda _tz, _n=None: datetime(2026, 10, 26, 10, 0))
    st = E.get_settings(db, ORG)
    st.enabled, st.app_base_url = 1, "https://hrmis.example.test"
    db.add(A(organization_id=ORG, provider="omnichannel", display_name="HTI",
             base_url="https://api.broadnet.test", access_token_enc=crypto.encrypt("tok"),
             token_last4="tok", webhook_token="a" * 43, status="active"))
    db.flush()
    seeds.seed_defaults(db, ORG, user_id=1)
    for tpl in db.query(T).all():
        tpl.approval_status = "approved"
    db.commit()
    return db


def _inbound(frm, text="Show list", shape="button"):
    msg = {"from": frm, "id": "wamid.1", "type": shape}
    msg[shape] = {"text": text} if shape == "button" else {"body": text}
    return {"entry": [{"changes": [{"field": "messages", "value": {"messages": [msg]}}]}]}


def _tap(db, monkeypatch, frm):
    fake = FakeProvider()
    monkeypatch.setattr("app.whatsapp_layer.providers.registry.get_provider", lambda _n: fake)
    webhooks.process(db, db.query(A).one(), {}, json.dumps(_inbound(frm)).encode())
    return fake


def test_reminder_prompt_is_one_message_whatever_the_list_size(org):
    items = [_item(100 + i, 7, f"Candidate With A Long Name {i}") for i in range(14)]
    E.evaluate_org_reminders(org, ORG, items, NOW, dispatch=lambda _i: None)
    log = org.query(L).one()
    assert log.template_name == "clawback_daily_prompt" and log.params["due_count"] == "14"
    assert len(log.health_check_ids) == 14
    assert "Tap Show list" in log.rendered_body and "/clawback/today" in log.rendered_body


def test_team_prompt_is_one_message(org, monkeypatch):
    prompt = org.query(T).filter(T.name == "clawback_team_prompt").one()
    for rule in org.query(DR).all():
        rule.template_id = prompt.id
    org.commit()
    D.run_org_digests(org, ORG, NOW, dispatch=lambda _i: None, items=[])
    assert org.query(L).count() == 0                  # nothing due, no message
    monkeypatch.setattr("app.whatsapp_layer.clients.fetch_subordinates", lambda _o, aud, **_k: (
        [{"user_id": 50, "name": "Meera Iyer", "subordinate_user_ids": [7, 8],
          "direct_team_user_ids": [7, 8]}] if aud == "team_managers" else []))
    items = [_item(100 + i, 7, f"Candidate With A Long Name {i}") for i in range(14)]
    D.run_org_digests(org, ORG, NOW, dispatch=lambda _i: None, items=items)
    log = org.query(L).one()
    assert log.template_name == "clawback_team_prompt" and log.params["due_count"] == "14"
    assert log.params["part_label"] == "14 candidates" and "/clawback/today" in log.rendered_body


def test_parse_inbound_button_and_text():
    p = OmnichannelProvider(http=object(), timeout=1)
    ev = p.parse_webhook({}, _inbound("919876543210"))[0]
    assert (ev.kind, ev.text, ev.recipient) == ("inbound", "Show list", "919876543210")
    assert p.parse_webhook({}, _inbound("91", "list", "text"))[0].text == "list"


def test_tap_sends_the_recruiter_only_their_formatted_list(org, monkeypatch):
    fake = _tap(org, monkeypatch, "919876543210")
    assert len(fake.texts) == 1
    to, text = fake.texts[0]
    assert to == "919876543210"
    assert "*1. Ravi Kumar*\nAcme\n" in text and "\n\n*2. Priya Shah*" in text
    assert "To do: Weekly check-in call" in text and "/hc/" in text
    assert "Imran Ali" not in text
    log = org.query(L).one()
    assert log.status == "submitted" and log.rendered_body == text and log.recipient_user_id == 7


def test_tap_by_manager_groups_by_recruiter(org, monkeypatch):
    _, text = _tap(org, monkeypatch, "919800000050").texts[0]
    assert text.index("Asha Rao (2)") < text.index("Ravi Kumar") < text.index("Vikram Das (1)") \
        < text.index("Imran Ali")


def test_repeated_webhook_replies_once(org, monkeypatch):
    fake = _tap(org, monkeypatch, "919876543210")
    webhooks.process(org, org.query(A).one(), {}, json.dumps(_inbound("919876543210")).encode())
    assert len(fake.texts) == 1


def test_unknown_number_and_other_words_get_nothing(org, monkeypatch):
    assert _tap(org, monkeypatch, "919999999999").texts == []
    fake = FakeProvider()
    monkeypatch.setattr("app.whatsapp_layer.providers.registry.get_provider", lambda _n: fake)
    webhooks.process(org, org.query(A).one(), {},
                     json.dumps(_inbound("919876543210", "hello", "text")).encode())
    assert fake.texts == []


def test_long_list_splits_into_valid_text_messages():
    cands = [{"status": "pending", "candidate_name": f"Name {i}", "company_name": "Acme",
              "progress": "joined 12 Oct, day 14 of 90", "action": "Call", "check_id": i,
              "link": f"https://x.test/hc/{i}"} for i in range(1, 201)]
    msgs = TL.render_list([{"recruiter_name": "Asha", "candidates": cands}], True)
    assert len(msgs) > 1 and all(len(m) <= 4096 for m in msgs)
    assert sum(m.count("/hc/") for m in msgs) == 200


def test_nothing_open_says_so():
    assert "Nothing to do" in TL.render_list([], False)[0]


def _request(headers=(), query=b""):
    return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers],
                    "query_string": query})


def test_webhook_token_found_in_header_or_query_only_if_correct(org):
    good = "a" * 43
    assert webhook_api._token_account(org, _request([("x-webhook-token", good)])) is not None
    assert webhook_api._token_account(org, _request([("authorization", f"Bearer {good}")])) is not None
    assert webhook_api._token_account(org, _request(query=f"hub.verify_token={good}".encode())) is not None
    assert webhook_api._token_account(org, _request([("x-webhook-token", "b" * 43)])) is None
    assert webhook_api._token_account(org, _request()) is None


def test_signed_webhook_needs_a_signature_made_with_the_token(org):
    import hashlib, hmac
    raw = b'{"entry": []}'
    good = hmac.new(b"a" * 43, raw, hashlib.sha256).hexdigest()
    assert webhook_api._signed_account(org, f"sha256={good}", raw) is not None
    assert webhook_api._signed_account(org, good.upper(), raw) is not None
    bad = hmac.new(b"wrong", raw, hashlib.sha256).hexdigest()
    assert webhook_api._signed_account(org, f"sha256={bad}", raw) is None
    assert webhook_api._signed_account(org, "", raw) is None
    assert webhook_api._signed_account(org, f"sha256={good}", b"changed") is None


def test_unverified_call_is_kept_but_not_acted_on(org):
    from app.whatsapp_layer.models import WhatsAppWebhookEvent as W
    webhook_api._store_unverified(org, {"x-hub-signature-256": "sha256=x", "authorization": "secret"},
                                  json.dumps(_inbound("919876543210")).encode())
    ev = org.query(W).one()
    assert ev.processed == 0 and ev.error == "unverified" and ev.account_id is None
    assert "authorization" not in ev.headers and org.query(L).count() == 0


def test_today_gives_progress_and_days_pending(org):
    data = TL.build_today(org, ORG, {7})
    ravi = data["groups"][0]["candidates"][0]
    assert ravi["joined_at"] == "2026-10-19" and ravi["cooling_period_days"] == 90
    assert ravi["days_pending"] == 83 and ravi["progress_percent"] == 8
    assert ravi["completion_date"] == "2027-01-17"
