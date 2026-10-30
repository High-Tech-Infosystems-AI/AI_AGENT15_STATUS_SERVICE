"""Sync imports templates from the provider and keeps statuses current;
response shapes are the live BroadNet ones (GET /whatsapp/template/list)."""
import json

import pytest

from app.whatsapp_layer import crypto
from app.whatsapp_layer import templates_service as ts
from app.whatsapp_layer.models import WhatsAppProviderAccount as A, WhatsAppTemplate as T
from app.whatsapp_layer.providers.base import ProviderError, ProviderUnsupported
from app.whatsapp_layer.providers.omnichannel import OmnichannelProvider

ORG = 12
DAILY = {
    "id": "1599", "name": "clawback_daily_checklist", "language": "en_US", "category": "UTILITY",
    "status": "APPROVED", "footerText": None,
    "headerContentJson": json.dumps({"type": "TEXT", "text": "Today's clawback check-ins"}),
    "bodyText": "Hi {{recruiter_name}} ({{part_label}}):\r\n{{candidate_list}}\r\nPending: "
                "{{overdue_count}}. Open each link.\r\n",
    "bodyExampleJson": json.dumps([{"example": "Asha Rao", "param_name": "recruiter_name"}]),
}
TEAM = {**DAILY, "id": "1600", "name": "clawback_team_checklist", "status": "PENDING",
        "headerContentJson": None,
        "bodyText": "Hi {{recipient_name}} ({{part_label}}): {{candidate_list}} done.",
        "bodyExampleJson": None}


class FakeProvider:
    def __init__(self, templates=(), error=None):
        self.templates, self.error = [OmnichannelProvider.parse_template(t) for t in templates], error

    def list_templates(self, creds, category=None):
        if self.error:
            raise self.error
        return self.templates

    def get_template(self, creds, name, language):
        return next((t for t in self.templates if t.name == name), None)


@pytest.fixture
def account(wa_db):
    a = A(organization_id=ORG, provider="omnichannel", display_name="HTI",
          base_url="https://api.broadnet.test", access_token_enc=crypto.encrypt("tok"),
          token_last4="tok", webhook_token="wh1", status="active")
    wa_db.add(a)
    wa_db.commit()
    return a


def _use(monkeypatch, provider):
    monkeypatch.setattr("app.whatsapp_layer.providers.registry.get_provider", lambda _name: provider)


def test_imports_missing_templates_ready_to_send(wa_db, account, monkeypatch):
    _use(monkeypatch, FakeProvider([DAILY, TEAM]))
    res = ts.sync_templates(wa_db, ORG)
    assert res["imported"] == 2 and res["failed"] == 0
    daily = wa_db.query(T).filter(T.name == "clawback_daily_checklist").one()
    assert daily.approval_status == "approved" and daily.purpose == "recruiter_batch"
    assert daily.account_id == account.id and daily.provider_template_id == "1599"
    assert daily.header_format == "TEXT" and daily.header_text == "Today's clawback check-ins"
    assert daily.body_text_named.endswith("Open each link.") and "\r" not in daily.body_text_named
    assert daily.variable_map["body"] == ["recruiter_name", "part_label", "candidate_list", "overdue_count"]
    assert daily.sample_values == {"recruiter_name": "Asha Rao"}
    team = wa_db.query(T).filter(T.name == "clawback_team_checklist").one()
    assert team.approval_status == "pending" and team.purpose == "team_digest"


def test_second_sync_imports_nothing_and_updates_status(wa_db, account, monkeypatch):
    _use(monkeypatch, FakeProvider([TEAM]))
    ts.sync_templates(wa_db, ORG)
    _use(monkeypatch, FakeProvider([{**TEAM, "status": "APPROVED"}]))
    res = ts.sync_templates(wa_db, ORG)
    assert res["imported"] == 0 and res["updated"] == 1
    assert wa_db.query(T).one().approval_status == "approved"


def test_local_draft_takes_the_provider_body(wa_db, account, monkeypatch):
    tpl = T(organization_id=ORG, name="clawback_daily_checklist", language="en_US",
            approval_status="draft", purpose="generic", body_text_named="Old {{x}} text.")
    wa_db.add(tpl)
    wa_db.commit()
    _use(monkeypatch, FakeProvider([DAILY]))
    assert ts.sync_templates(wa_db, ORG)["imported"] == 0
    assert tpl.approval_status == "approved" and "candidate_list" in tpl.variable_map["body"]
    assert wa_db.query(T).count() == 1


def test_provider_error_is_reported_not_raised(wa_db, account, monkeypatch):
    _use(monkeypatch, FakeProvider(error=ProviderError("boom", status_code=500)))
    assert ts.sync_templates(wa_db, ORG)["failed"] == 1


def test_no_list_api_is_quiet(wa_db, account, monkeypatch):
    _use(monkeypatch, FakeProvider(error=ProviderUnsupported("no", status_code=404)))
    res = ts.sync_templates(wa_db, ORG)
    assert res["failed"] == 0 and res["imported"] == 0


def test_inactive_account_not_called(wa_db, account, monkeypatch):
    account.status = "disabled"
    wa_db.commit()
    _use(monkeypatch, FakeProvider(error=ProviderError("should not be called")))
    assert ts.sync_templates(wa_db, ORG)["failed"] == 0


def test_sync_records_named_format_for_sending(wa_db, account, monkeypatch):
    _use(monkeypatch, FakeProvider([{**DAILY, "parameterFormat": "NAMED"}]))
    ts.sync_templates(wa_db, ORG)
    tpl = wa_db.query(T).one()
    assert ts.body_param_names(tpl) == ["recruiter_name", "part_label", "candidate_list", "overdue_count"]
    tpl.parameter_format = None
    assert ts.body_param_names(tpl) == []


def test_sync_backfills_format_on_existing_template(wa_db, account, monkeypatch):
    _use(monkeypatch, FakeProvider([DAILY]))
    ts.sync_templates(wa_db, ORG)
    assert wa_db.query(T).one().parameter_format is None
    _use(monkeypatch, FakeProvider([{**DAILY, "parameterFormat": "NAMED"}]))
    ts.sync_templates(wa_db, ORG)
    assert wa_db.query(T).one().parameter_format == "NAMED"


def test_multi_variable_send_matches_the_portal_payload(wa_db, account, monkeypatch):
    """Values and order as captured from the BroadNet portal for this template."""
    from app.whatsapp_layer.providers.base import TemplateRef
    listing = ("1) Ravi Kumar – Acme Retail – joined 12 Oct, day 14 of 90 – Weekly check-in call – "
               "https://devai.htinfosystems.com/hc/101 · 2) Priya Shah – Zen Foods – joined 27 Oct, "
               "day 3 of 90 – First-week call – https://devai.htinfosystems.com/hc/102 · 3) Imran Ali "
               "– Acme Retail – offer accepted 20 Oct, not joined yet – Call to confirm joining – "
               "https://devai.htinfosystems.com/hc/103")
    _use(monkeypatch, FakeProvider([{**DAILY, "parameterFormat": "NAMED"}]))
    ts.sync_templates(wa_db, ORG)
    tpl = wa_db.query(T).one()
    _, body = ts.send_variables(tpl.variable_map, {
        "recruiter_name": "Asha Rao", "part_label": "3 candidates",
        "candidate_list": listing, "overdue_count": "2"})
    fields = dict((k, v[1]) for k, v in OmnichannelProvider.build_send_fields(
        "916304457168", TemplateRef(tpl.name, tpl.language, ts.body_param_names(tpl)), [], body, "cbk_1"))
    assert fields["bodyTemplateVariables[0]"] == '{"parameter_name":"recruiter_name","text":"Asha Rao"}'
    assert fields["bodyTemplateVariables[1]"] == '{"parameter_name":"part_label","text":"3 candidates"}'
    assert fields["bodyTemplateVariables[2]"] == '{"parameter_name":"candidate_list","text":"%s"}' % listing
    assert fields["bodyTemplateVariables[3]"] == '{"parameter_name":"overdue_count","text":"2"}'
    assert "bodyTemplateVariables[4]" not in fields
