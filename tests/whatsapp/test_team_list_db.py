"""Team summaries list today's candidates under each recruiter (contract
§6.4): grouping, links to health checks, organisation scope, parts."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import crypto, seeds
from app.whatsapp_layer import digest_engine as D
from app.whatsapp_layer.models import (
    ClawbackDigestRule as DR, ClawbackHealthCheck as H, WhatsAppMessageLog as L,
    WhatsAppProviderAccount as A, WhatsAppTemplate as T,
)

ORG = 12
TODAY = date(2026, 10, 26)                                      # Monday
NOW = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)        # 10:00 IST
PHONES = {7: "+919876543210", 8: "+919811112222", 50: "+919800000050", 60: "+919800000060"}
NAMES = {7: "Asha Rao", 8: "Vikram Das", 50: "Meera Iyer", 60: "Org Admin"}


def _item(cj, rec, name=None, joined_days_ago=7):
    joined = TODAY - timedelta(days=joined_days_ago)
    return {"candidate_job_id": cj, "candidate_job_status_id": cj + 1000, "status": "in_cooling",
            "organization_id": ORG, "candidate_id": f"C{cj}", "candidate_name": name or f"Cand {cj}",
            "job_id": 9, "job_title": "Sales Executive", "company_name": "Acme",
            "offer_accepted_at": None, "joined_at": joined.isoformat(), "cooling_period_days": 90,
            "clawback_completion_date": (joined + timedelta(days=90)).isoformat(),
            "cooling_period_closed": None, "recruiter_user_id": rec, "assigned_to": rec,
            "created_by": 3}


@pytest.fixture
def org(wa_db, monkeypatch):
    db = wa_db
    monkeypatch.setattr("app.whatsapp_layer.people.get_users", lambda _db, ids: {
        int(i): {"id": int(i), "name": NAMES.get(int(i), f"User {i}"), "email": f"u{i}@x",
                 "organization_id": ORG, "phone_e164": PHONES.get(int(i)), "enabled": True}
        for i in ids if i})
    audiences = {"team_managers": [{"user_id": 50, "name": "Meera Iyer",
                                    "subordinate_user_ids": [7, 8], "direct_team_user_ids": [7, 8]}],
                 "has_subordinate_managers": [],
                 "users": [{"user_id": 60, "name": "Org Admin", "subordinate_user_ids": [],
                            "direct_team_user_ids": []}]}
    monkeypatch.setattr("app.whatsapp_layer.clients.fetch_subordinates",
                        lambda _org, audience, **kw: audiences[audience])
    monkeypatch.setattr("app.whatsapp_layer.people.admin_users", lambda _db, _org: [])
    st = E.get_settings(db, ORG)
    st.enabled = 1
    st.app_base_url = "https://hrmis.example.test"
    db.add(A(organization_id=ORG, provider="omnichannel", display_name="HTI",
             base_url="https://api.broadnet.test", access_token_enc=crypto.encrypt("tok"),
             token_last4="tok", webhook_token="wh1", status="active"))
    db.flush()
    seeds.seed_defaults(db, ORG, user_id=1)
    for tpl in db.query(T).all():
        tpl.approval_status = "draft" if tpl.name.endswith("_prompt") else "approved"
    list_tpl = db.query(T).filter(T.name == "clawback_team_checklist").one()
    for rule in db.query(DR).all():
        rule.template_id = list_tpl.id
    db.commit()
    return db


def test_team_lead_gets_candidates_grouped_by_recruiter(org):
    items = [_item(1, 7, "Ravi Kumar"), _item(2, 7, "Priya Shah"), _item(3, 8, "Imran Ali")]
    stats = D.run_org_digests(org, ORG, NOW, items=items)
    assert stats["queued"] == 1
    log = org.query(L).one()
    checks = {c.candidate_job_id: c.id for c in org.query(H).all()}
    assert len(checks) == 3                       # created so the summary can link to them
    assert log.recipient_user_id == 50 and log.template_name == "clawback_team_checklist"
    assert log.params["candidate_list"] == (
        f"Asha Rao (2): 1) Ravi Kumar – Acme – joined 19 Oct, day 8 of 90 – "
        f"https://hrmis.example.test/hc/{checks[1]} · 2) Priya Shah – Acme – joined 19 Oct, "
        f"day 8 of 90 – https://hrmis.example.test/hc/{checks[2]} | Vikram Das (1): "
        f"1) Imran Ali – Acme – joined 19 Oct, day 8 of 90 – https://hrmis.example.test/hc/{checks[3]}")
    assert log.params["part_label"] == "3 candidates" and log.params["recruiter_count"] == "2"
    assert log.rendered_body.startswith(
        "Hi Meera Iyer, here are today's clawback check-ins for your team (3 candidates)")

    # The recruiters' own run later reuses the same checks.
    E.evaluate_org_reminders(org, ORG, items, NOW + timedelta(minutes=30))
    assert org.query(H).count() == 3


def test_organisation_scope_for_admins(org):
    org.add(DR(organization_id=ORG, name="Admins – daily", audience_type="users",
               audience_value={"user_ids": [60]}, scope="organisation", frequency_type="daily",
               frequency_value={}, send_time="09:30", skip_if_empty=1, priority=30, is_active=1))
    org.commit()
    D.run_org_digests(org, ORG, NOW, items=[_item(1, 7), _item(2, 8), _item(3, 99)])
    admin = org.query(L).filter(L.recipient_user_id == 60).one()
    assert admin.params["part_label"] == "3 candidates"
    assert "Asha Rao (1): " in admin.params["candidate_list"]
    assert "User 99 (1): " in admin.params["candidate_list"]


def test_long_team_list_split_into_parts(org):
    items = [_item(100 + i, 7 if i % 2 else 8, f"Candidate With A Long Name {i}")
             for i in range(14)]
    D.run_org_digests(org, ORG, NOW, items=items)
    logs = org.query(L).filter(L.recipient_user_id == 50).order_by(L.id).all()
    assert len(logs) > 1
    assert logs[0].params["part_label"] == f"14 candidates, part 1 of {len(logs)}"
    assert all(len(l.rendered_body) <= 1024 for l in logs)
    assert sum(l.params["candidate_list"].count("/hc/") for l in logs) == 14
    # Re-run: nothing new.
    D.run_org_digests(org, ORG, NOW + timedelta(minutes=15), items=items)
    assert org.query(L).filter(L.recipient_user_id == 50).count() == len(logs)


def test_nothing_due_and_nothing_missed_is_skipped(org):
    stats = D.run_org_digests(org, ORG, NOW, items=[_item(1, 7, joined_days_ago=8)])  # day 9
    assert stats["empty"] == 1 and org.query(L).count() == 0


def test_nothing_due_today_is_skipped_even_with_missed_checks_and_skip_off(org):
    org.add(H(organization_id=ORG, candidate_job_id=1, recruiter_user_id=7,
              due_date=TODAY - timedelta(days=1), status="missed"))
    for rule in org.query(DR).all():
        rule.skip_if_empty = 0
    org.commit()
    sent = []
    stats = D.run_org_digests(org, ORG, NOW, dispatch=sent.append,
                              items=[_item(1, 7, joined_days_ago=8)])     # day 9: nothing due
    assert stats["empty"] == 1 and stats["queued"] == 0
    assert org.query(L).count() == 0 and sent == []


def test_manager_without_phone_gets_no_message(org, monkeypatch):
    monkeypatch.setitem(PHONES, 50, None)
    sent = []
    stats = D.run_org_digests(org, ORG, NOW, dispatch=sent.append, items=[_item(1, 7)])
    assert stats["queued"] == 0 and stats["skipped"] == 1 and sent == []
    log = org.query(L).one()
    assert log.status == "skipped" and log.skip_reason == "no_phone"


def test_recruiter_without_phone_gets_no_message(org, monkeypatch):
    monkeypatch.setitem(PHONES, 7, None)
    sent = []
    E.evaluate_org_reminders(org, ORG, [_item(1, 7), _item(2, 8)], NOW, dispatch=sent.append)
    logs = {l.recipient_user_id: l for l in org.query(L).filter(L.kind != "digest").all()}
    assert logs[7].status == "skipped" and logs[7].skip_reason == "no_phone"
    assert logs[8].status == "queued" and sent == [logs[8].id]


def test_recruiter_with_nothing_due_gets_no_message(org):
    sent = []
    E.evaluate_org_reminders(org, ORG, [_item(1, 7, joined_days_ago=8)], NOW, dispatch=sent.append)
    assert org.query(L).count() == 0 and sent == []


def test_admins_get_the_whole_organisation_by_role(org, monkeypatch):
    monkeypatch.setattr("app.whatsapp_layer.people.admin_users", lambda _db, _org: [
        {"user_id": 60, "name": "Org Admin", "subordinate_user_ids": [],
         "direct_team_user_ids": []},
        {"user_id": 50, "name": "Meera Iyer", "subordinate_user_ids": [],
         "direct_team_user_ids": []}])
    D.run_org_digests(org, ORG, NOW, items=[_item(1, 7), _item(2, 8), _item(3, 99)])
    logs = {l.recipient_user_id: l for l in org.query(L).all()}
    assert set(logs) == {50, 60}
    for uid in (50, 60):                        # admin who is also a team lead: org-wide
        assert logs[uid].params["part_label"] == "3 candidates"
        assert "User 99 (1): " in logs[uid].params["candidate_list"]
    assert logs[60].rule_name == "Admins – daily"


def test_candidate_without_recruiter_is_logged_as_skipped(org):
    item = _item(1, None)
    item["created_by"] = None
    sent = []
    E.evaluate_org_reminders(org, ORG, [item], NOW, dispatch=sent.append)
    log = org.query(L).one()
    assert log.status == "skipped" and log.skip_reason == "no_recruiter"
    assert log.candidate_name == "Cand 1" and sent == []
    E.evaluate_org_reminders(org, ORG, [item], NOW + timedelta(minutes=15))
    assert org.query(L).count() == 1
