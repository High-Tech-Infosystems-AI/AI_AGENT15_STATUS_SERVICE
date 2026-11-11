"""Template status / quality / category changes and Flow alerts: one path for
the webhook and the polling sync, notifying the submitter + admins once.
Payloads are the exact ones Omnichannel forwarded on staging."""
import copy

import pytest

from app.whatsapp_layer import campaign_forward, flow_alerts, sender, template_status as TS, webhooks
from app.whatsapp_layer import templates_service as ts
from app.whatsapp_layer.models import (
    WhatsAppMessageLog, WhatsAppProviderAccount as A, WhatsAppTemplate as T,
)
from app.whatsapp_layer.providers.base import ProviderTemplate
from app.whatsapp_layer.providers.omnichannel import OmnichannelProvider

ORG = 12

# Exactly as received on staging (template status update).
STAGING_TEMPLATE_APPROVED = {
    "entry": [{"id": "102938475610293", "time": 1793010000, "changes": [{
        "field": "message_template_status_update",
        "value": {"event": "APPROVED", "reason": "NONE",
                  "message_template_id": 1864204150837425,
                  "message_template_name": "clawback_eod_report",
                  "message_template_category": "UTILITY",
                  "message_template_language": "en_US"}}]}],
    "object": "whatsapp_business_account",
}

# Exactly as received on staging (message status).
STAGING_MESSAGE_STATUS = {
    "entry": [{"id": "102938475610293", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "919000000000", "phone_number_id": "1111"},
        "statuses": [{
            "id": "wamid.HBgMOTE5ODc2NTQzMjEwFQIAERgSQUFBQUFBQUFBQUFBQUFBQUFBAA==",
            "status": "delivered", "timestamp": "1793010100", "recipient_id": "919876543210",
            "pricing": {"billable": True, "pricing_model": "PMP", "category": "utility",
                        "type": "regular"},
            "biz_opaque_callback_data":
                "{\"msg_id\":5012,\"username\":\"hti_user\",\"batch_id\":2611111136064561001}"}]}}]}],
    "object": "whatsapp_business_account",
}

# Exactly as received on staging (92 of these were ignored before).
STAGING_FLOW_ALERT = {
    "entry": [{"id": "102938475610293", "time": 1793010200, "changes": [{
        "field": "flows",
        "value": {"event": "ENDPOINT_AVAILABILITY", "flow_id": "1287364519283746",
                  "message": "Flow endpoint availability was below 90% threshold in the last "
                             "60 minutes. Users are unable to open or use the flow.",
                  "threshold": 90, "alert_state": "ACTIVATED", "availability": 0}}]}],
    "object": "whatsapp_business_account",
}

# Meta's documented shapes (not yet seen from Omnichannel).
META_QUALITY_UPDATE = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
    "field": "message_template_quality_update",
    "value": {"previous_quality_score": "GREEN", "new_quality_score": "RED",
              "message_template_id": 1864204150837425,
              "message_template_name": "clawback_eod_report",
              "message_template_language": "en_US"}}]}]}
META_CATEGORY_UPDATE = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
    "field": "template_category_update",
    "value": {"message_template_id": 1864204150837425,
              "message_template_name": "clawback_eod_report",
              "message_template_language": "en_US",
              "previous_category": "UTILITY", "new_category": "MARKETING"}}]}]}


def _status_payload(event, reason="NONE", **extra):
    p = copy.deepcopy(STAGING_TEMPLATE_APPROVED)
    p["entry"][0]["changes"][0]["value"].update({"event": event, "reason": reason, **extra})
    return p


class FakeRedis:
    def __init__(self):
        self.keys = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def delete(self, *keys):
        n = sum(1 for k in keys if k in self.keys)
        for k in keys:
            self.keys.pop(k, None)
        return n


@pytest.fixture
def sent(monkeypatch):
    """Real dedupe on a fake Redis; notifications recorded instead of stored."""
    redis = FakeRedis()
    monkeypatch.setattr("app.notification_layer.redis_manager.get_notification_redis",
                        lambda: redis)
    calls = []

    def fake_notify(db, org, **kw):
        calls.append(kw)
        return len(calls)
    monkeypatch.setattr("app.whatsapp_layer.alerts.notify_org_admins", fake_notify)
    return calls


@pytest.fixture
def account(wa_db):
    a = A(organization_id=ORG, provider="omnichannel", display_name="HTI",
          base_url="https://x", webhook_token="tok-tpl", status="active")
    wa_db.add(a)
    wa_db.commit()
    return a


@pytest.fixture
def tpl(wa_db, account):
    from datetime import datetime
    t = T(organization_id=ORG, account_id=account.id, name="clawback_eod_report",
          language="en_US", category="UTILITY", body_text_named="x", approval_status="pending",
          provider_template_id="1864204150837425", created_by=7, updated_by=9,
          submitted_at=datetime.utcnow())
    wa_db.add(t)
    wa_db.commit()
    return t


def _hook(db, account, payload):
    return webhooks.apply_events(db, account, OmnichannelProvider().parse_webhook({}, payload))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parse_staging_template_status_payload():
    [e] = OmnichannelProvider().parse_webhook({}, STAGING_TEMPLATE_APPROVED)
    assert (e.kind, e.status, e.event) == ("template_status", "approved", "APPROVED")
    assert e.provider_template_id == "1864204150837425"
    assert e.template_name == "clawback_eod_report" and e.template_language == "en_US"
    assert e.reason is None and e.category == "UTILITY"


@pytest.mark.parametrize("event,status", [
    ("APPROVED", "approved"), ("REJECTED", "rejected"), ("PENDING", "pending"),
    ("PAUSED", "paused"), ("DISABLED", "disabled"), ("FLAGGED", "approved"),
    ("PENDING_DELETION", "disabled"), ("DELETED", "disabled"), ("REINSTATED", "approved"),
    ("IN_APPEAL", "pending"),
])
def test_every_meta_template_event_maps_to_our_status(event, status):
    [e] = OmnichannelProvider().parse_webhook({}, _status_payload(event))
    assert e.status == status and e.event == event


def test_parse_staging_message_status_reads_opaque_batch_id():
    [e] = OmnichannelProvider().parse_webhook({}, STAGING_MESSAGE_STATUS)
    assert e.kind == "message_status" and e.status == "delivered"
    assert e.provider_message_id.startswith("wamid.") and e.recipient == "919876543210"
    assert e.batch_id == "2611111136064561001"


def test_parse_staging_flow_alert():
    [e] = OmnichannelProvider().parse_webhook({}, STAGING_FLOW_ALERT)
    assert (e.kind, e.event, e.flow_id, e.alert_state) == (
        "flow_event", "ENDPOINT_AVAILABILITY", "1287364519283746", "ACTIVATED")
    assert e.reason.startswith("Flow endpoint availability was below 90%")


def test_parse_quality_and_category_updates():
    [q] = OmnichannelProvider().parse_webhook({}, META_QUALITY_UPDATE)
    assert (q.kind, q.quality_score, q.previous_quality_score) == ("template_quality", "RED", "GREEN")
    [c] = OmnichannelProvider().parse_webhook({}, META_CATEGORY_UPDATE)
    assert (c.kind, c.category, c.previous_category) == ("template_category", "MARKETING", "UTILITY")
    assert c.provider_template_id == "1864204150837425"


def test_paused_reason_comes_from_other_info():
    p = _status_payload("PAUSED", other_info={"title": "FIRST_PAUSE",
                                              "description": "Paused for 3 hours: low quality"})
    [e] = OmnichannelProvider().parse_webhook({}, p)
    assert e.status == "paused" and e.reason == "Paused for 3 hours: low quality"


def test_staging_message_status_matches_log_by_opaque_batch_id(wa_db, account):
    log = WhatsAppMessageLog(organization_id=ORG, account_id=account.id, kind="reminder",
                             status="submitted", attempts=1, idempotency_key="k1",
                             provider_ref="2611111136064561001", phone_e164="+910000000000")
    wa_db.add(log)
    wa_db.commit()
    assert _hook(wa_db, account, STAGING_MESSAGE_STATUS) == [log.id]
    assert log.status == "delivered"


# ---------------------------------------------------------------------------
# Template status through the webhook
# ---------------------------------------------------------------------------
def test_approved_webhook_notifies_submitter_and_admins_with_link(wa_db, account, tpl, sent):
    _hook(wa_db, account, STAGING_TEMPLATE_APPROVED)
    assert tpl.approval_status == "approved"
    [n] = sent
    assert n["title"] == "WhatsApp template approved"
    assert "'clawback_eod_report' was approved" in n["message"]
    assert "clawback and campaigns" in n["message"]
    assert set(n["user_ids"]) == {7, 9} and n["event"] == "alert_template_status"
    assert n["metadata"]["link"] == f"/settings/channels/whatsapp/{account.id}/templates/{tpl.id}"


def test_same_webhook_twice_notifies_once(wa_db, account, tpl, sent):
    _hook(wa_db, account, STAGING_TEMPLATE_APPROVED)
    _hook(wa_db, account, STAGING_TEMPLATE_APPROVED)
    assert len(sent) == 1


def test_webhook_then_sync_notifies_once(wa_db, account, tpl, sent):
    _hook(wa_db, account, STAGING_TEMPLATE_APPROVED)
    ptpl = ProviderTemplate(name=tpl.name, language="en_US", status="approved")
    assert ts.apply_provider_status(tpl, ptpl, wa_db) is False
    assert len(sent) == 1


def test_racing_webhook_and_sync_dedupe_in_redis(wa_db, account, tpl, sent):
    """Both saw `pending` and both wrote `approved`: the Redis claim wins once."""
    TS.apply_change(wa_db, tpl, TS.TemplateChange(status="approved", source="sync"))
    tpl.approval_status = "pending"          # the other worker's stale view
    TS.apply_change(wa_db, tpl, TS.TemplateChange(status="approved", source="webhook"))
    assert len(sent) == 1


def test_sync_alone_notifies_like_the_webhook(wa_db, account, tpl, sent):
    ptpl = ProviderTemplate(name=tpl.name, language="en_US", status="approved")
    assert ts.apply_provider_status(tpl, ptpl, wa_db) is True
    assert [n["title"] for n in sent] == ["WhatsApp template approved"]


def test_rejected_with_reason(wa_db, account, tpl, sent):
    _hook(wa_db, account, _status_payload("REJECTED", "INVALID_FORMAT"))
    assert tpl.approval_status == "rejected" and tpl.rejection_reason == "INVALID_FORMAT"
    [n] = sent
    assert n["event"] == "alert_template_rejected"
    assert "rejected: the format is invalid" in n["message"]


def test_paused_reinstated_paused_notifies_each_transition(wa_db, account, tpl, sent):
    tpl.approval_status = "approved"
    wa_db.commit()
    _hook(wa_db, account, _status_payload("PAUSED"))
    assert tpl.approval_status == "paused"
    _hook(wa_db, account, _status_payload("REINSTATED"))
    assert tpl.approval_status == "approved"
    _hook(wa_db, account, _status_payload("PAUSED"))
    assert [n["title"] for n in sent] == ["WhatsApp template paused", "WhatsApp template reinstated",
                                          "WhatsApp template paused"]
    assert "will not send until it is reinstated" in sent[0]["message"]


def test_disabled_tells_what_to_do(wa_db, account, tpl, sent):
    _hook(wa_db, account, _status_payload("DISABLED"))
    assert tpl.approval_status == "disabled"
    assert "Create a new version" in sent[0]["message"]


def test_pending_and_draft_transitions_are_quiet(wa_db, account, tpl, sent):
    tpl.approval_status = "rejected"
    wa_db.commit()
    _hook(wa_db, account, _status_payload("PENDING"))
    assert tpl.approval_status == "pending" and sent == []
    tpl.approval_status = "draft"
    wa_db.commit()
    _hook(wa_db, account, _status_payload("APPROVED"))
    assert tpl.approval_status == "approved" and sent == []


def test_flagged_keeps_it_sendable_and_warns_on_quality(wa_db, account, tpl, sent):
    tpl.approval_status = "approved"
    wa_db.commit()
    _hook(wa_db, account, _status_payload("FLAGGED"))
    assert tpl.approval_status == "approved" and tpl.quality_score == "RED"
    assert [n["title"] for n in sent] == ["WhatsApp template quality dropped"]


def test_unknown_template_is_ignored(wa_db, account, sent):
    p = _status_payload("APPROVED")
    p["entry"][0]["changes"][0]["value"].update(message_template_id=1, message_template_name="nope")
    _hook(wa_db, account, p)
    assert sent == []


def test_matches_by_name_when_meta_id_unknown(wa_db, account, tpl, sent):
    tpl.provider_template_id = None
    wa_db.commit()
    _hook(wa_db, account, STAGING_TEMPLATE_APPROVED)
    assert tpl.approval_status == "approved"


# ---------------------------------------------------------------------------
# Quality / category
# ---------------------------------------------------------------------------
def test_quality_drop_stored_and_notified_once(wa_db, account, tpl, sent):
    tpl.approval_status = "approved"
    wa_db.commit()
    _hook(wa_db, account, META_QUALITY_UPDATE)
    _hook(wa_db, account, META_QUALITY_UPDATE)
    assert tpl.quality_score == "RED"
    assert len(sent) == 1 and "low quality (red)" in sent[0]["message"]
    assert ts.serialize(tpl)["quality_score"] == "RED"


def test_quality_recovery_is_quiet_then_new_drop_notifies(wa_db, account, tpl, sent):
    for score in ("YELLOW", "GREEN", "YELLOW"):
        p = copy.deepcopy(META_QUALITY_UPDATE)
        p["entry"][0]["changes"][0]["value"]["new_quality_score"] = score
        _hook(wa_db, account, p)
    assert tpl.quality_score == "YELLOW"
    assert [n["metadata"]["value"] for n in sent] == ["YELLOW", "YELLOW"]


def test_category_update_sets_previous_category_and_notifies(wa_db, account, tpl, sent):
    tpl.approval_status = "approved"
    wa_db.commit()
    _hook(wa_db, account, META_CATEGORY_UPDATE)
    assert tpl.category == "MARKETING" and tpl.previous_category == "UTILITY"
    [n] = sent
    assert n["title"] == "WhatsApp template re-categorised" and "UTILITY to MARKETING" in n["message"]


def test_category_advance_notice_does_not_change_category(wa_db, account, tpl, sent):
    p = copy.deepcopy(META_CATEGORY_UPDATE)
    v = p["entry"][0]["changes"][0]["value"]
    v.pop("new_category")
    v["correct_category"] = "MARKETING"
    _hook(wa_db, account, p)
    assert tpl.category == "UTILITY"
    assert [n["title"] for n in sent] == ["WhatsApp template will be re-categorised"]


# ---------------------------------------------------------------------------
# Flow alerts
# ---------------------------------------------------------------------------
def test_flow_alert_notifies_admins_once_until_resolved(wa_db, account, sent):
    for _ in range(3):   # Meta repeats it (92 arrived on staging)
        _hook(wa_db, account, STAGING_FLOW_ALERT)
    assert len(sent) == 1
    n = sent[0]
    assert n["title"] == "WhatsApp Flow endpoint unavailable"
    assert "availability was below 90%" in n["message"]
    assert n["event"] == "alert_flow_endpoint" and "user_ids" not in n
    assert n["metadata"]["link"] == f"/settings/channels/whatsapp/{account.id}?tab=flows"

    resolved = copy.deepcopy(STAGING_FLOW_ALERT)
    resolved["entry"][0]["changes"][0]["value"]["alert_state"] = "DEACTIVATED"
    _hook(wa_db, account, resolved)
    _hook(wa_db, account, resolved)          # nothing left to clear
    assert [n["title"] for n in sent][1:] == ["WhatsApp Flow endpoint unavailable — resolved"]
    _hook(wa_db, account, STAGING_FLOW_ALERT)  # fires again after clearing
    assert len(sent) == 3


def test_flow_status_change_blocked(wa_db, account, sent):
    from app.whatsapp_layer.providers.base import WebhookEvent
    ev = WebhookEvent(kind="flow_event", event="FLOW_STATUS_CHANGE", flow_id="55",
                      raw={"old_status": "PUBLISHED", "new_status": "BLOCKED"})
    assert flow_alerts.handle(wa_db, account, ev) == 1
    ev.raw = {"old_status": "DRAFT", "new_status": "PUBLISHED"}
    assert flow_alerts.handle(wa_db, account, ev) is None


def test_flow_and_template_events_are_forwarded_to_campaigns():
    p = OmnichannelProvider()
    assert campaign_forward.is_campaign_relevant(p.parse_webhook({}, STAGING_FLOW_ALERT))
    assert campaign_forward.is_campaign_relevant(p.parse_webhook({}, META_QUALITY_UPDATE))
    assert campaign_forward.is_campaign_relevant(p.parse_webhook({}, META_CATEGORY_UPDATE))


# ---------------------------------------------------------------------------
# Batch status: the live server answers 400 for a finished batch
# ---------------------------------------------------------------------------
DONE_400 = ("Cannot edit batch 2611111136064561001 — current status is DONE "
            "(allowed: PENDING / INPROGRESS / PAUSED).")


class _Resp:
    def __init__(self, code, body=None, text=""):
        self.status_code, self._body, self.text = code, body, text

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


class _Http:
    def __init__(self, resp):
        self.resp = resp

    def request(self, *a, **k):
        return self.resp


def _creds():
    from app.whatsapp_layer.providers.base import ProviderCredentials
    return ProviderCredentials(base_url="https://x", access_token="t")


@pytest.mark.parametrize("resp", [
    _Resp(400, {"message": DONE_400}), _Resp(400, None, text=DONE_400),
    _Resp(400, {"error": DONE_400.replace("DONE", "Done")}),
])
def test_finished_batch_400_means_sent(resp):
    st = OmnichannelProvider(http=_Http(resp)).get_send_status(_creds(), "2611111136064561001")
    assert st.status == "sent" and st.processed is None


@pytest.mark.parametrize("word", ["FAILED", "ABORTED", "CANCELLED"])
def test_failed_batch_400_means_failed(word):
    resp = _Resp(400, {"message": DONE_400.replace("DONE", word)})
    st = OmnichannelProvider(http=_Http(resp)).get_send_status(_creds(), "1")
    assert st.status == "failed" and word in st.error_message


def test_other_400_still_raises():
    from app.whatsapp_layer.providers.base import ProviderError
    with pytest.raises(ProviderError):
        OmnichannelProvider(http=_Http(_Resp(400, {"message": "Bad batch id"}))).get_send_status(
            _creds(), "1")


def test_poll_batches_marks_finished_batch_sent(wa_db, monkeypatch):
    from datetime import datetime, timedelta
    from app.whatsapp_layer import crypto
    acct = A(organization_id=ORG, provider="omnichannel", display_name="HTI", base_url="https://x",
             access_token_enc=crypto.encrypt("tok"), token_last4="tok", webhook_token="w",
             status="active")
    wa_db.add(acct)
    wa_db.flush()
    log = WhatsAppMessageLog(organization_id=ORG, account_id=acct.id, kind="reminder",
                             status="submitted", attempts=1, idempotency_key="k150",
                             provider_ref="2611111136064561001",
                             submitted_at=datetime.utcnow() - timedelta(minutes=10))
    wa_db.add(log)
    wa_db.commit()
    provider = OmnichannelProvider(http=_Http(_Resp(400, {"message": DONE_400})))
    monkeypatch.setattr("app.whatsapp_layer.providers.registry.get_provider", lambda _n: provider)
    assert sender.poll_batches(wa_db) == 1
    assert log.status == "sent" and log.sent_at is not None
    assert acct.status == "active"
