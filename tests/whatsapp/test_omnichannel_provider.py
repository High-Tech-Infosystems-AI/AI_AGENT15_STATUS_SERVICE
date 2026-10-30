"""Omnichannel (BroadNet) adapter against a fake HTTP session — exact
multipart field names and response parsing. No network."""
import json

import pytest
import requests

from app.whatsapp_layer.providers.base import (
    ProviderCredentials, ProviderError, ProviderUnsupported, TemplateRef, TemplateSpec,
)
from app.whatsapp_layer.providers.omnichannel import OmnichannelProvider

CREDS = ProviderCredentials(base_url="https://api.broadnet.test/v1/", access_token="tok-123")


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Records every request; returns queued responses or raises queued errors."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _provider(*responses):
    session = FakeSession(*responses)
    return OmnichannelProvider(http=session, timeout=5), session


def _send(provider):
    return provider.send_template(CREDS, "919876543210", TemplateRef("clawback_health_check_due", "en_US"),
                                  [], ["Asha", "Ravi K", "14"], "cbk_91")


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------
def test_send_request_exact_multipart_fields():
    provider, session = _provider(FakeResponse(200, {"totalNumbersSuccess": 1, "totalNumbersFailed": 0,
                                                     "totalNumbersDuplicate": 0,
                                                     "batchId": "2602020728123242061"}))
    _send(provider)
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.broadnet.test/v1/whatsapp/message/sendbulk"
    assert call["headers"]["Authorization"] == "Bearer tok-123"
    assert call["timeout"] == 5
    fields = {k: v[1] for k, v in call["files"]}
    assert all(v[0] is None for _, v in call["files"])  # no file parts
    assert fields == {
        "messageType": "TEMPLATE",
        "destinationNo": "919876543210",
        "templateName": "clawback_health_check_due",
        "templateLanguage": "en_US",
        "batchId": "0",
        "campaignName": "cbk_91",
        "allowDuplicate": "true",
        "bodyTemplateVariables[0]": "Asha",
        "bodyTemplateVariables[1]": "Ravi K",
        "bodyTemplateVariables[2]": "14",
    }


def test_header_variables_are_a_json_array_string():
    fields = dict((k, v[1]) for k, v in OmnichannelProvider.build_send_fields(
        "91999", TemplateRef("t", "en_US"), ["Ravi"], [], "cbk_1"))
    assert fields["headerTemplateVariables"] == '["Ravi"]'
    assert not any(k.startswith("bodyTemplateVariables") for k in fields)


def test_create_template_fields():
    spec = TemplateSpec(name="clawback_check", language="en_US", category="UTILITY",
                        header_format="TEXT", header_text="For {{1}}", header_example=["Ravi"],
                        body_text="Hi {{1}}, check {{2}}.", body_example=["Asha", "Ravi"],
                        footer_text="HTI HRMIS",
                        buttons=[{"type": "URL", "label": "Open", "url": "https://hrmis.test/x"},
                                 {"type": "QUICK_REPLY", "label": "Done"}])
    provider, session = _provider(FakeResponse(200, {
        "id": "1223818762524529", "name": "clawback_check", "language": "en_US",
        "category": "UTILITY", "status": "PENDING", "previousCategory": None,
        "rejectionReason": None}))
    result = provider.create_template(CREDS, spec)
    call = session.calls[0]
    assert call["url"] == "https://api.broadnet.test/v1/whatsapp/template"
    pairs = [(k, v[1]) for k, v in call["files"]]
    f = dict(pairs)
    assert f["name"] == "clawback_check" and f["languageType"] == "en_US"
    assert f["category"] == "UTILITY" and f["templateType"] == "DEFAULT" and f["subCategory"] == "DEFAULT"
    assert f["headerFormat"] == "TEXT" and f["headerText"] == "For {{1}}"
    assert f["headerExample"] == '["Ravi"]'
    assert f["bodyText"] == "Hi {{1}}, check {{2}}."
    assert f["bodyExample"] == '[["Asha", "Ravi"]]'
    assert f["footerText"] == "HTI HRMIS"
    assert [v for k, v in pairs if k == "buttonRequest"] == ["URL:Open|https://hrmis.test/x", "QUICK_REPLY:Done"]
    assert result.provider_template_id == "1223818762524529" and result.status == "pending"


# ---------------------------------------------------------------------------
# Send response parsing
# ---------------------------------------------------------------------------
def test_send_success():
    provider, _ = _provider(FakeResponse(200, {"totalFilesProcessed": 0, "totalNumbersSuccess": 1,
                                               "totalNumbersFailed": 0, "totalNumbersDuplicate": 0,
                                               "batchId": "2601221336333031021", "files": []}))
    r = _send(provider)
    assert r.accepted and r.provider_ref == "2601221336333031021"
    assert not r.duplicate and not r.retryable and not r.auth_error


def test_send_failed_number_is_final():
    provider, _ = _provider(FakeResponse(200, {"totalNumbersSuccess": 0, "totalNumbersFailed": 1,
                                               "totalNumbersDuplicate": 0, "batchId": "1"}))
    r = _send(provider)
    assert not r.accepted and not r.retryable and r.error_code == "provider_failed"


def test_send_duplicate():
    provider, _ = _provider(FakeResponse(200, {"totalNumbersSuccess": 0, "totalNumbersFailed": 0,
                                               "totalNumbersDuplicate": 1, "batchId": "0"}))
    r = _send(provider)
    assert r.duplicate and not r.accepted and r.provider_ref is None


def test_send_401_is_auth_error():
    provider, _ = _provider(FakeResponse(401, {"message": "Unauthorized"}))
    r = _send(provider)
    assert r.auth_error and not r.retryable and r.error_code == "401"


@pytest.mark.parametrize("code", [500, 502, 503, 429])
def test_send_5xx_is_retryable(code):
    provider, _ = _provider(FakeResponse(code, None, text="upstream down"))
    r = _send(provider)
    assert r.retryable and not r.accepted and r.error_code == str(code)


def test_send_4xx_is_final():
    provider, _ = _provider(FakeResponse(400, {"message": "Template not found"}))
    r = _send(provider)
    assert not r.retryable and r.error_message == "Template not found"


def test_send_timeout_and_network_errors_are_retryable():
    provider, _ = _provider(requests.Timeout("slow"), requests.ConnectionError("refused"))
    assert _send(provider).retryable
    assert _send(provider).retryable


def test_send_non_json_200_is_retryable():
    provider, _ = _provider(FakeResponse(200, None, text="<html>"))
    r = _send(provider)
    assert not r.accepted and r.retryable


# ---------------------------------------------------------------------------
# Batch status / health / templates
# ---------------------------------------------------------------------------
def test_batch_status_done():
    provider, session = _provider(FakeResponse(200, {"batchId": 2602020728123242061, "status": "Done",
                                                     "processedCount": 1, "totalCount": 1,
                                                     "pendingCount": 0}))
    st = provider.get_send_status(CREDS, "2602020728123242061")
    assert st.status == "sent" and st.processed == 1
    assert session.calls[0]["params"] == {"batch_Id": "2602020728123242061"}
    assert session.calls[0]["url"].endswith("/whatsapp/message/edit")


def test_batch_status_pending_and_failed():
    assert OmnichannelProvider.parse_batch_status({"status": "Running", "processedCount": 0,
                                                   "pendingCount": 1}).status == "pending"
    assert OmnichannelProvider.parse_batch_status({"status": "Done", "processedCount": 0}).status == "failed"
    assert OmnichannelProvider.parse_batch_status({"content": [{"status": "Done", "processedCount": 1}]}).status == "sent"


def test_health_check_parsing():
    provider, _ = _provider(FakeResponse(200, {
        "wabaId": "987654321098765", "phoneNumberId": "102290129340398",
        "displayPhoneNumber": "+1 980-123-4567", "qualityRating": "GREEN", "status": "ACTIVE",
        "currentLimit": "TIER_1K", "sandbox": False, "accessToken": "EAAJ..."}))
    h = provider.health_check(CREDS)
    assert h.ok and h.quality_rating == "GREEN" and h.messaging_limit == "TIER_1K"
    assert h.sandbox is False and h.waba_id == "987654321098765"
    assert "accessToken" not in h.raw


def test_health_check_401():
    provider, _ = _provider(FakeResponse(401, {"message": "bad token"}))
    h = provider.health_check(CREDS)
    assert not h.ok and h.auth_error


def test_template_read_404_is_unsupported():
    provider, _ = _provider(FakeResponse(404, {"message": "no route"}))
    with pytest.raises(ProviderUnsupported):
        provider.get_template(CREDS, "x", "en_US")


def test_get_template_maps_status():
    provider, _ = _provider(FakeResponse(200, {"id": "1", "name": "x", "language": "en_US",
                                               "status": "REJECTED", "rejectionReason": "INVALID_FORMAT"}))
    t = provider.get_template(CREDS, "x", "en_US")
    assert t.status == "rejected" and t.rejection_reason == "INVALID_FORMAT"


def test_create_template_401_raises_auth_error():
    provider, _ = _provider(FakeResponse(401, {"message": "nope"}))
    with pytest.raises(ProviderError) as exc:
        provider.create_template(CREDS, TemplateSpec(name="x", language="en_US", body_text="hi"))
    assert exc.value.auth_error


def test_get_template_uses_name_param_and_not_found_is_none():
    provider, http = _provider(FakeResponse(404, {"message": "Template Not fount", "errorCode": "NOT_FOUND"}))
    assert provider.get_template(CREDS, "x", "en_US") is None
    call = http.calls[0]
    assert call["url"].endswith("/whatsapp/template") and call["params"] == {"name": "x"}


def test_list_templates_reads_every_page():
    provider, http = _provider(
        FakeResponse(200, {"data": [{"name": "a"}], "lastPage": False}),
        FakeResponse(200, {"data": [{"name": "b"}], "lastPage": True}))
    assert [t.name for t in provider.list_templates(CREDS)] == ["a", "b"]
    assert [c["params"]["page"] for c in http.calls] == [0, 1]
    assert http.calls[0]["url"].endswith("/whatsapp/template/list")


def test_named_template_sends_parameter_name_and_text():
    fields = dict((k, v[1]) for k, v in OmnichannelProvider.build_send_fields(
        "91999", TemplateRef("t", "en_US", ["recruiter_name", "part_label"]),
        [], ["Supriyo Chowdhury", "3 candidates – part 1"], "cbk_1"))
    assert json.loads(fields["bodyTemplateVariables[0]"]) == {
        "parameter_name": "recruiter_name", "text": "Supriyo Chowdhury"}
    assert fields["bodyTemplateVariables[1]"] == \
        '{"parameter_name": "part_label", "text": "3 candidates – part 1"}'


def test_parse_template_reads_parameter_format():
    assert OmnichannelProvider.parse_template({"parameterFormat": "NAMED"}).parameter_format == "NAMED"
