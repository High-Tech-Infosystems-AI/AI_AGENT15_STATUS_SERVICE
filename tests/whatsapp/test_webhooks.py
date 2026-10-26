"""Webhook mapping (UNCONFIRMED payload format): Meta Cloud API-style
payloads → events → log status updates (SQLite)."""
import copy
import time
from datetime import datetime, timedelta

from app.whatsapp_layer import sender, webhooks
from app.whatsapp_layer.models import WhatsAppMessageLog, WhatsAppProviderAccount, WhatsAppTemplate
from app.whatsapp_layer.providers.omnichannel import OmnichannelProvider

META_STATUS_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "831797373346873",
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15550001111", "phone_number_id": "102290129340398"},
                "statuses": [
                    {"id": "wamid.AAA", "status": "delivered", "timestamp": "1793000000",
                     "recipient_id": "919876543210"},
                    {"id": "wamid.BBB", "status": "read", "timestamp": "1793000100",
                     "recipient_id": "919800000001"},
                    {"id": "wamid.CCC", "status": "failed", "timestamp": "1793000200",
                     "recipient_id": "919800000002",
                     "errors": [{"code": 131026, "title": "Message undeliverable"}]},
                ],
            },
        }],
    }],
}

META_TEMPLATE_PAYLOAD = {
    "entry": [{"changes": [{"field": "message_template_status_update", "value": {
        "event": "REJECTED", "message_template_id": 1223818762524529,
        "message_template_name": "clawback_health_check_due",
        "message_template_language": "en_US", "reason": "INVALID_FORMAT"}}]}],
}


def test_parse_meta_status_payload():
    events = OmnichannelProvider().parse_webhook({}, META_STATUS_PAYLOAD)
    assert [(e.kind, e.provider_message_id, e.status) for e in events] == [
        ("message_status", "wamid.AAA", "delivered"),
        ("message_status", "wamid.BBB", "read"),
        ("message_status", "wamid.CCC", "failed"),
    ]
    assert events[0].recipient == "919876543210" and events[0].timestamp == 1793000000
    assert events[2].error_code == "131026" and events[2].error_message == "Message undeliverable"


def test_parse_template_status_payload_and_garbage():
    events = OmnichannelProvider().parse_webhook({}, META_TEMPLATE_PAYLOAD)
    assert len(events) == 1
    e = events[0]
    assert e.kind == "template_status" and e.status == "rejected"
    assert e.provider_template_id == "1223818762524529" and e.reason == "INVALID_FORMAT"
    assert OmnichannelProvider().parse_webhook({}, None) == []
    assert OmnichannelProvider().parse_webhook({}, {"hello": "world"}) == []


def test_flat_payload_with_campaign_name():
    events = OmnichannelProvider().parse_webhook({}, {"campaignName": "cbk_5", "statuses": [
        {"status": "sent", "recipient_id": "91999"}]})
    assert events[0].campaign_name == "cbk_5" and events[0].status == "sent"


def _log(db, **kw):
    now = datetime.utcnow()
    base = dict(organization_id=12, kind="reminder", status="submitted", attempts=1,
                submitted_at=now - timedelta(minutes=10), created_at=now - timedelta(minutes=10))
    base.update(kw)
    log = WhatsAppMessageLog(**base)
    db.add(log)
    db.flush()
    return log


def test_apply_events_matches_logs(wa_db, monkeypatch):
    db = wa_db
    sent_alerts = []
    monkeypatch.setattr("app.whatsapp_layer.alerts.notify_org_admins",
                        lambda *a, **k: sent_alerts.append(k.get("dedupe_key")))
    acct = WhatsAppProviderAccount(organization_id=12, provider="omnichannel", display_name="HTI",
                                   base_url="https://x", webhook_token="tok123", status="active")
    db.add(acct)
    db.flush()
    by_wamid = _log(db, idempotency_key="k1", provider_message_id="wamid.AAA", phone_e164="+919876543210")
    # No wamid stored: matched by phone + most recent submitted within 24 h.
    old = _log(db, idempotency_key="k2", phone_e164="+919800000001",
               submitted_at=datetime.utcnow() - timedelta(hours=3))
    recent = _log(db, idempotency_key="k3", phone_e164="+919800000001")
    by_phone_failed = _log(db, idempotency_key="k4", phone_e164="+919800000002")
    other_org = _log(db, idempotency_key="k5", organization_id=99, provider_message_id="wamid.CCC",
                     phone_e164="+919800000002")
    tpl = WhatsAppTemplate(organization_id=12, name="clawback_health_check_due", language="en_US",
                           body_text_named="x", approval_status="pending",
                           provider_template_id="1223818762524529")
    db.add(tpl)
    db.commit()

    # Event timestamps "now", so the phone fallback's 24 h window applies.
    payload = copy.deepcopy(META_STATUS_PAYLOAD)
    for st in payload["entry"][0]["changes"][0]["value"]["statuses"]:
        st["timestamp"] = str(int(time.time()))
    events = OmnichannelProvider().parse_webhook({}, payload)
    events += OmnichannelProvider().parse_webhook({}, META_TEMPLATE_PAYLOAD)
    changed = webhooks.apply_events(db, acct, events)

    assert by_wamid.status == "delivered" and by_wamid.delivered_at is not None
    assert recent.status == "read" and recent.provider_message_id == "wamid.BBB"
    assert old.status == "submitted"
    assert by_phone_failed.status == "failed" and by_phone_failed.error_code == "131026"
    assert other_org.status == "submitted"   # never crosses orgs
    assert set(changed) == {by_wamid.id, recent.id, by_phone_failed.id}
    assert tpl.approval_status == "rejected" and tpl.rejection_reason == "INVALID_FORMAT"
    assert sent_alerts == [f"template_rejected:{tpl.id}"]


def test_status_never_moves_backwards():
    log = WhatsAppMessageLog(status="read")
    assert not sender.advance_status(log, "delivered", datetime.utcnow())
    assert not sender.advance_status(log, "failed", datetime.utcnow())
    log = WhatsAppMessageLog(status="submitted")
    assert sender.advance_status(log, "delivered", datetime.utcnow())
    assert log.sent_at is not None and log.delivered_at is not None


def test_process_stores_raw_event_even_when_unparseable(wa_db):
    acct = WhatsAppProviderAccount(organization_id=12, provider="omnichannel", display_name="HTI",
                                   base_url="https://x", webhook_token="tok9", status="active")
    wa_db.add(acct)
    wa_db.commit()
    webhooks.process(wa_db, acct, {"Content-Type": "text/plain", "Authorization": "secret"}, b"not json")
    from app.whatsapp_layer.models import WhatsAppWebhookEvent
    ev = wa_db.query(WhatsAppWebhookEvent).one()
    assert ev.body is None and ev.body_raw == "not json" and ev.processed == 1
    assert "Authorization" not in ev.headers
    assert webhooks.find_account(wa_db, "omnichannel", "tok9").id == acct.id
    assert webhooks.find_account(wa_db, "omnichannel", "nope") is None
