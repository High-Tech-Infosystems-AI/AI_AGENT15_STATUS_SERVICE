"""Engine DB glue end-to-end on SQLite with the mock provider: health checks,
batching, idempotent re-runs, skips, retries, polling, cancellation, missed."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import crypto, sender, seeds
from app.whatsapp_layer.models import (
    ClawbackHealthCheck as H, WhatsAppMessageLog as L, WhatsAppProviderAccount as A,
    WhatsAppTemplate as T,
)

ORG = 12
TODAY = date(2026, 10, 26)                                      # Monday
NOW = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)         # 10:30 IST
PHONES = {7: "+919876543210", 8: "+919811112222", 9: None, 10: "+919800005000"}


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
def org(wa_db, monkeypatch):
    db = wa_db
    monkeypatch.setattr("app.whatsapp_layer.people.get_users", lambda _db, ids: {
        int(i): {"id": int(i), "name": f"User {i}", "email": f"u{i}@x", "organization_id": ORG,
                 "phone_e164": PHONES.get(int(i)), "enabled": True} for i in ids if i})
    monkeypatch.setattr("app.whatsapp_layer.alerts.notify_org_admins", lambda *a, **k: None)
    st = E.get_settings(db, ORG)
    st.enabled = 1
    st.app_base_url = "https://hrmis.example.test"
    db.add(A(organization_id=ORG, provider="omnichannel", display_name="HTI",
             base_url="https://api.broadnet.test", access_token_enc=crypto.encrypt("tok"),
             token_last4="tok", webhook_token="wh1", status="active"))
    db.flush()
    seeds.seed_defaults(db, ORG, user_id=1)
    for tpl in db.query(T).all():
        tpl.approval_status = "draft" if tpl.name.endswith(("_prompt", "_names")) else "approved"
    db.commit()
    return db


def test_reminders_list_per_recruiter_and_idempotent_rerun(org):
    db = org
    items = [_item(1, 7)] + [_item(10 + i, 8) for i in range(4)]   # day 8 → "Days 8–30"
    stats = E.evaluate_org_reminders(db, ORG, items, NOW)
    assert stats == {"checks": 5, "queued": 2, "skipped": 0}
    assert db.query(H).count() == 5
    logs = db.query(L).order_by(L.id).all()
    # Even a single candidate goes out as that recruiter's list.
    assert [(l.kind, l.recipient_user_id, l.status) for l in logs] == [
        ("batch_reminder", 7, "submitted"), ("batch_reminder", 8, "submitted")]
    single, batch = logs
    chk = db.query(H).filter(H.candidate_job_id == 1).one()
    joined = (TODAY - timedelta(days=7))
    assert single.params["candidate_list"] == (
        f"1) Cand 1 – Acme – joined {joined.day} Oct, day 8 of 90 – Weekly check-in call – "
        f"https://hrmis.example.test/hc/{chk.id}")
    assert single.params["part_label"] == "1 candidate"
    assert single.rendered_body.startswith("Hi User 7, here are your clawback check-ins for today")
    assert single.provider_ref and single.correlation_id == f"cbk_{single.id}"
    assert batch.params["due_count"] == "4" and len(batch.health_check_ids) == 4
    assert batch.params["candidate_list"].count("/hc/") == 4
    assert batch.template_name == "clawback_daily_checklist"

    # Re-running the same day creates nothing new.
    assert E.evaluate_org_reminders(db, ORG, items, NOW + timedelta(minutes=15)) == \
        {"checks": 5, "queued": 0, "skipped": 0}
    assert db.query(L).count() == 2 and db.query(H).count() == 5


def test_nothing_before_send_time(org):
    early = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)   # 08:30 IST
    assert E.evaluate_org_reminders(org, ORG, [_item(1, 7)], early)["checks"] == 0
    assert org.query(H).count() == 0


def test_non_working_day_creates_checks_but_sends_nothing(org):
    # Sunday 25 Oct (not in the default MON–SAT working days), Day 8 of the clawback.
    sunday = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    sunday_items = [_item(1, 7)]
    sunday_items[0]["joined_at"] = (date(2026, 10, 25) - timedelta(days=7)).isoformat()
    sunday_items[0]["clawback_completion_date"] = None
    stats = E.evaluate_org_reminders(org, ORG, sunday_items, sunday)
    assert stats["checks"] == 1 and stats["queued"] == 0
    assert org.query(L).count() == 0


def test_skip_reasons_and_retry(org):
    db = org
    items = [_item(1, 9), _item(2, 10)]   # 9 has no phone; 10 → mock retryable 5xx
    E.evaluate_org_reminders(db, ORG, items, NOW)
    by_rec = {l.recipient_user_id: l for l in db.query(L).all()}
    assert by_rec[9].status == "skipped" and by_rec[9].skip_reason == "no_phone"
    retry = by_rec[10]
    assert retry.status == "queued" and retry.attempts == 1 and retry.next_attempt_at is not None
    # Each retry fails again (mock) → 1, 5, 15 minute backoff, then failed.
    for _ in range(3):
        sender.send_log(db, retry.id, now=retry.next_attempt_at)
    assert retry.status == "failed" and retry.attempts == 4


def test_template_not_approved_and_account_inactive(org):
    db = org
    for tpl in db.query(T).all():
        tpl.approval_status = "pending"
    db.commit()
    E.evaluate_org_reminders(db, ORG, [_item(1, 7)], NOW)
    assert db.query(L).one().skip_reason == "template_not_approved"

    for tpl in db.query(T).all():
        tpl.approval_status = "draft" if tpl.name.endswith(("_prompt", "_names")) else "approved"
    db.query(A).update({A.status: "disabled"})
    db.commit()
    E.evaluate_org_reminders(db, ORG, [_item(2, 8)], NOW)
    assert db.query(L).filter(L.recipient_user_id == 8).one().skip_reason == "account_inactive"


def test_poll_marks_sent(org):
    db = org
    E.evaluate_org_reminders(db, ORG, [_item(1, 7)], NOW)
    log = db.query(L).one()
    log.submitted_at = datetime.utcnow() - timedelta(minutes=5)
    db.commit()
    assert sender.poll_batches(db) == 1
    assert log.status == "sent" and log.sent_at is not None


def test_final_validation_and_cancel_on_state_change(org, monkeypatch):
    db = org
    final_item = _item(5, 7, joined_days_ago=90)     # completion date = today
    E.evaluate_org_reminders(db, ORG, [final_item], NOW)
    log = db.query(L).one()
    chk = db.query(H).one()
    assert log.kind == "batch_reminder"
    assert log.params["candidate_list"].endswith(
        f"cooling period ended 26 Oct – Final status validation – https://hrmis.example.test/hc/{chk.id}")
    assert chk.anchor == "cooling_end" and chk.day_number == 0 and chk.status == "pending"

    res = E.handle_clawback_state_changed(db, {"candidate_job_id": 5, "organization_id": ORG,
                                               "change": "completed"})
    assert res["cancelled"] == 1 and chk.status == "cancelled"


def test_mark_missed_after_cutoff(org):
    db = org
    E.evaluate_org_reminders(db, ORG, [_item(1, 7)], NOW)
    assert E.mark_missed(db, NOW) == 0                               # 10:30 IST < 23:00
    late = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)       # 23:30 IST
    assert E.mark_missed(db, late) == 1
    assert db.query(H).one().status == "missed"


def test_disabled_org_does_nothing(org):
    st = E.get_settings(org, ORG)
    st.enabled = 0
    org.commit()
    assert E.evaluate_org_reminders(org, ORG, [_item(1, 7)], NOW) == {"checks": 0, "queued": 0, "skipped": 0}


def test_reset_defaults_replaces_rules(org):
    db = org
    rules, digests = seeds.seed_defaults(db, ORG, user_id=1, replace=True)
    db.commit()
    assert len(rules) == 7 and len(digests) == 3
    assert len(E.active_rules(db, ORG)) == 7
    assert db.query(T).filter(T.deleted_at.is_(None)).count() == 10   # templates not duplicated


def test_long_list_split_into_parts(org):
    db = org
    items = [_item(100 + i, 8) for i in range(12)]
    for it in items:
        it["candidate_name"] = f"Candidate With A Long Name {it['candidate_job_id']}"
        it["company_name"] = "Acme Retail Private Limited"
    E.evaluate_org_reminders(db, ORG, items, NOW)
    logs = db.query(L).order_by(L.id).all()
    assert len(logs) > 1 and all(l.kind == "batch_reminder" for l in logs)
    assert logs[0].params["part_label"] == f"12 candidates, part 1 of {len(logs)}"
    assert sum(len(l.health_check_ids) for l in logs) == 12
    assert all(len(l.params["candidate_list"]) <= 700 for l in logs)
    assert all(len(l.rendered_body) <= 1024 for l in logs)
    assert logs[1].params["candidate_list"].startswith(f"{len(logs[0].health_check_ids) + 1}) ")
    # Re-running sends no part twice.
    E.evaluate_org_reminders(db, ORG, items, NOW + timedelta(minutes=15))
    assert db.query(L).count() == len(logs)


def test_list_off_sends_one_message_per_candidate(org):
    db = org
    st = E.get_settings(db, ORG)
    st.event_toggles = {"recruiter_summary": False}
    db.commit()
    E.evaluate_org_reminders(db, ORG, [_item(1, 7), _item(2, 7)], NOW)
    logs = db.query(L).order_by(L.id).all()
    assert [l.kind for l in logs] == ["reminder", "reminder"]
    assert logs[0].params["check_action"] == "Weekly check-in call"
