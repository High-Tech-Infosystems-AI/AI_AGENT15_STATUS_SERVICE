"""Campaign events are forwarded to the Campaign Service; clawback ones are not."""
from app.core import settings
from app.whatsapp_layer import campaign_forward
from app.whatsapp_layer.providers.base import WebhookEvent


def _status(campaign_name):
    return WebhookEvent(kind="message_status", status="delivered", campaign_name=campaign_name)


def test_relevance_rules():
    assert campaign_forward.is_campaign_relevant([_status("cmp12_m345")])
    assert campaign_forward.is_campaign_relevant([WebhookEvent(kind="inbound", text="STOP")])
    assert campaign_forward.is_campaign_relevant([WebhookEvent(kind="template_status", status="approved")])
    assert not campaign_forward.is_campaign_relevant([_status("cbk_99")])
    assert not campaign_forward.is_campaign_relevant([_status(None)])
    assert not campaign_forward.is_campaign_relevant([])


def test_forward_posts_raw_body_with_internal_headers(monkeypatch):
    calls = []

    class _Resp:
        status_code = 202

    monkeypatch.setattr(settings, "CAMPAIGN_SERVICE_URL", "http://campaigns:8522/", raising=False)
    monkeypatch.setattr(settings, "INTERNAL_SERVICE_TOKEN", "tok", raising=False)
    monkeypatch.setattr(campaign_forward.requests, "post",
                        lambda url, data, headers, timeout: calls.append((url, data, headers)) or _Resp())

    raw = b'{"statuses":[{"campaignName":"cmp1_m2","status":"read"}]}'
    started = campaign_forward.forward(7, {"Content-Type": "application/json", "Authorization": "x"},
                                       raw, [_status("cmp1_m2")], background=False)
    assert started
    url, data, headers = calls[0]
    assert url == "http://campaigns:8522/campaigns/internal/events"
    assert data == raw
    assert headers["X-Internal-Token"] == "tok" and headers["X-Org-Id"] == "7"
    assert "Authorization" not in headers


def test_forward_off_without_config(monkeypatch):
    monkeypatch.setattr(settings, "CAMPAIGN_SERVICE_URL", "", raising=False)
    assert not campaign_forward.forward(1, {}, b"{}", [_status("cmp1_m1")], background=False)
