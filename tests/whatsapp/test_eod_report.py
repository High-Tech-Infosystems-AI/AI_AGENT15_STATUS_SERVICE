"""End of day report and the names variables (contract §6.9)."""
from datetime import date, datetime, timezone

import pytest

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import crypto, eod_report as R, seeds
from app.whatsapp_layer import templates_service as ts
from app.whatsapp_layer import variables as V
from app.whatsapp_layer.models import (
    ClawbackHealthCheck as H, WhatsAppMessageLog as L, WhatsAppProviderAccount as A, WhatsAppTemplate as T,
)

ORG = 12
TODAY = date(2026, 10, 26)
EVENING = datetime(2026, 10, 26, 13, 15, tzinfo=timezone.utc)   # 18:45 IST
AFTERNOON = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)  # 15:30 IST
NAMES = {7: "Asha Rao", 8: "Vikram Das", 50: "Meera Iyer", 60: "Org Admin"}
PHONES = {50: "+919800000050", 60: "+919800000060"}


def _c(cj, rec, name, status="pending", outcome=None):
    return {"candidate_job_id": cj, "candidate_name": name, "recruiter_user_id": rec,
            "status": status, "outcome": outcome}


def test_names_list_reads_as_a_sentence_and_caps():
    assert V.names_list(["A"]) == "A"
    assert V.names_list(["A", "B", "C"]) == "A, B and C"
    long = V.names_list([f"Candidate {i}" for i in range(50)], 60)
    assert len(long) <= 60 and long.endswith("more")


def test_report_counts_recruiters_untracked_and_flags():
    checks = [_c(1, 7, "Ravi", "done", "engaged"), _c(2, 7, "Priya", "done", "at_risk"),
              _c(3, 7, "Imran"), _c(4, 8, "Neha", "missed")]
    drops = [{"candidate_job_id": 5, "candidate_name": "Arjun", "label": "Dropped", "recruiter_user_id": 8}]
    r = R.build_report(checks, drops, None, NAMES)
    assert (r["done_count"], r["total_count"]) == ("2", "4")
    assert r["recruiter_progress"] == "Asha Rao 2 of 3 and Vikram Das 0 of 1"
    assert r["untracked_list"] == "Imran and Neha" and r["untracked_count"] == "2"
    assert r["flagged_list"] == "Arjun (Dropped) and Priya (At risk)" and r["flagged_count"] == "2"


def test_report_respects_scope_and_empty_days():
    checks = [_c(1, 7, "Ravi"), _c(2, 8, "Neha")]
    assert R.build_report(checks, [], {8}, NAMES)["untracked_list"] == "Neha"
    assert R.build_report(checks, [], {99}, NAMES) is None
    assert R.build_report([], [], None, NAMES) is None


def test_clean_day_says_none():
    r = R.build_report([_c(1, 7, "Ravi", "done", "engaged")], [], None, NAMES)
    assert r["untracked_list"] == "None" and r["flagged_list"] == "None"


@pytest.mark.parametrize("name", ["clawback_daily_names", "clawback_team_names", "clawback_eod_report"])
def test_new_templates_pass_whatsapp_rules(name):
    spec = next(t for t in seeds.DEFAULT_TEMPLATES if t["name"] == name)
    data = {"name": name, "language": "en_US", "category": "UTILITY", "purpose": spec["purpose"],
            "header_format": "TEXT", "header_text": spec["header_text"], "body_text": spec["body_text"],
            "buttons": spec.get("buttons") or [], "sample_values": seeds._sample_values(spec["body_text"])}
    assert ts.validate_template(data, for_submit=True) == []


@pytest.fixture
def org(wa_db, monkeypatch):
    db = wa_db
    monkeypatch.setattr("app.whatsapp_layer.people.get_users", lambda _d, ids: {
        int(i): {"id": int(i), "name": NAMES.get(int(i)), "organization_id": ORG,
                 "phone_e164": PHONES.get(int(i)), "enabled": True} for i in ids})
    monkeypatch.setattr("app.whatsapp_layer.people.user_names", lambda _d, ids: {int(i): NAMES.get(int(i)) for i in ids})
    monkeypatch.setattr("app.whatsapp_layer.people.admin_users", lambda _d, _o: [
        {"user_id": 60, "name": "Org Admin", "subordinate_user_ids": [], "direct_team_user_ids": []}])
    monkeypatch.setattr("app.whatsapp_layer.clients.fetch_subordinates", lambda _o, aud, **_k: (
        [{"user_id": 50, "name": "Meera Iyer", "subordinate_user_ids": [7], "direct_team_user_ids": [7]}]
        if aud == "team_managers" else []))
    monkeypatch.setattr(R, "fetch_drops", lambda *_a: [])
    st = E.get_settings(db, ORG)
    st.enabled, st.app_base_url = 1, "https://hrmis.example.test"
    db.add(A(organization_id=ORG, provider="omnichannel", display_name="HTI",
             base_url="https://api.broadnet.test", access_token_enc=crypto.encrypt("tok"),
             token_last4="tok", webhook_token="wh1", status="active"))
    db.flush()
    seeds.seed_defaults(db, ORG, user_id=1)
    for t in db.query(T).all():
        t.approval_status = "approved"
    db.add(H(organization_id=ORG, candidate_job_id=1, candidate_name="Ravi", recruiter_user_id=7,
             due_date=TODAY, status="pending"))
    db.add(H(organization_id=ORG, candidate_job_id=2, candidate_name="Neha", recruiter_user_id=8,
             due_date=TODAY, status="done", outcome="engaged"))
    db.commit()
    return db


def test_sends_once_after_the_time_to_each_scope(org):
    sent = []
    assert R.run_org_eod(org, ORG, AFTERNOON, dispatch=sent.append)["queued"] == 0
    assert R.run_org_eod(org, ORG, EVENING, dispatch=sent.append)["queued"] == 2
    logs = {l.recipient_user_id: l for l in org.query(L).all()}
    assert logs[60].params["total_count"] == "2"           # admin: the whole organisation
    assert logs[50].params["total_count"] == "1"           # team lead: their team only
    assert logs[50].params["untracked_list"] == "Ravi"
    assert "Not tracked (1): Ravi." in logs[50].rendered_body
    assert R.run_org_eod(org, ORG, EVENING, dispatch=sent.append)["queued"] == 0
    assert len(sent) == 2


def test_switched_off_sends_nothing(org):
    st = E.get_settings(org, ORG)
    st.event_toggles = {"eod_report": False}
    org.commit()
    assert R.run_org_eod(org, ORG, EVENING)["queued"] == 0 and org.query(L).count() == 0
