"""Contract §6 (v1.1): combined recruiter message (candidate_list /
candidate_count, threshold 1, recruiter_summary toggle) and Events on/off
(listing, PATCH, alerts respecting their toggles)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.dependencies.tenant_auth import AuthCtx
from app.whatsapp_layer import accounts, alerts, crypto, seeds, tasks
from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import events as EV
from app.whatsapp_layer import variables as V
from app.whatsapp_layer.api import events_api
from app.whatsapp_layer.models import (
    ClawbackDigestRule as D, ClawbackHealthCheck as H, ClawbackNotificationRule as R,
    WhatsAppMessageLog as L, WhatsAppProviderAccount as A, WhatsAppTemplate as T,
)
from app.whatsapp_layer.schemas import EventToggle

ORG = 12
TODAY = date(2026, 10, 26)                                      # Monday
NOW = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)         # 10:30 IST
PHONES = {7: "+919876543210", 8: "+919811112222"}


# ---------------------------------------------------------------------------
# candidate_list formatting (pure, contract §6.4)
# ---------------------------------------------------------------------------
LINK = "https://hrmis.example.test/hc/"


def _entry(n, name=None, company="Acme"):
    return V.candidate_list_entry(
        n, candidate_name=name or f"Cand {n}", company_name=company,
        progress=V.progress_text("doj", joining_date=date(2026, 10, 12), day_number=14,
                                 cooling_period_days=90),
        action="Weekly check-in call", link=f"{LINK}{n}")


def test_list_entry_format():
    assert _entry(1, "Ravi K") == (
        "1) Ravi K – Acme – joined 12 Oct, day 14 of 90 – Weekly check-in call – "
        "https://hrmis.example.test/hc/1")


def test_progress_text_per_anchor():
    assert V.progress_text("offer_accepted", offer_accepted_date=date(2026, 10, 20)) == \
        "offer accepted 20 Oct, not joined yet"
    assert V.progress_text("cooling_end", joining_date=date(2026, 10, 12),
                           completion_date=date(2027, 1, 10)) == \
        "joined 12 Oct, cooling period ended 10 Jan"
    assert V.short_date(date(2026, 1, 5)) == "5 Jan"


def test_team_entry_has_no_action():
    e = V.candidate_list_entry(2, candidate_name="Neha P", company_name="Zen", progress="p",
                               link="L")
    assert e == "2) Neha P – Zen – p – L"


def test_check_link():
    assert V.check_link("https://x/", 5) == "https://x/hc/5"
    assert V.check_link("https://x", None) == "https://x/clawback/health-checks"


def test_split_short_list_is_one_part():
    parts = V.split_list([(None, None, _entry(i)) for i in range(1, 4)])
    assert len(parts) == 1 and parts[0][1] == 3
    assert parts[0][0].count(" · ") == 2


def test_split_long_list_into_parts_under_cap():
    entries = [(None, None, _entry(i)) for i in range(1, 21)]
    parts = V.split_list(entries)
    assert len(parts) > 1
    assert sum(n for _t, n in parts) == 20
    assert all(len(t) <= V.CANDIDATE_LIST_MAX_CHARS for t, _n in parts)
    assert parts[1][0].startswith(f"{parts[0][1] + 1}) Cand")


def test_split_grouped_list_repeats_header_when_continued():
    rows = [("Asha (8)", "Asha (contd.)", _entry(i)) for i in range(1, 9)]
    rows += [("Vikram (1)", "Vikram (contd.)", _entry(1, "Neha"))]
    parts = V.split_list(rows, max_chars=500)
    assert parts[0][0].startswith("Asha (8): 1) Cand 1")
    assert parts[1][0].startswith("Asha (contd.): ")
    assert " | Vikram (1): 1) Neha" in parts[-1][0] or parts[-1][0].startswith("Vikram (1): ")


def test_split_huge_entry_is_cut():
    parts = V.split_list([(None, None, _entry(1, "X" * 900)), (None, None, _entry(2))])
    assert len(parts) == 2 and parts[0][0].endswith("…") and parts[0][1] == 1
    assert len(parts[0][0]) <= V.CANDIDATE_LIST_MAX_CHARS


def test_split_sanitises_text():
    e = V.candidate_list_entry(1, candidate_name="Ravi\nK\t  Kumar     X\x07",
                               company_name="  Acme    Ltd ", progress="p", link="L")
    (value, _n), = V.split_list([(None, None, e)])
    assert value == "1) Ravi K Kumar X – Acme Ltd – p – L"


def test_part_label():
    assert V.part_label(1, 1, 1) == "1 candidate"
    assert V.part_label(9, 2, 3) == "9 candidates, part 2 of 3"


def test_candidate_list_parts_keeps_records():
    entries = [{"item": {"candidate_job_id": i, "candidate_name": f"Cand {i}",
                         "job_title": "SE", "company_name": "Acme", "cooling_period_days": 90,
                         "joined_at": "2026-10-12"},
                "anchor": "doj", "day": 10, "check": None, "rule": None} for i in range(1, 13)]
    parts = E.candidate_list_parts(entries, "https://x")
    assert sum(len(p["candidates"]) for p in parts) == 12
    assert all(p["candidate_count"] == "12" for p in parts)
    assert parts[-1]["candidates"][-1]["candidate_job_id"] == 12


def test_catalogue_has_list_variables():
    for key in ("candidate_list", "part_label", "due_count"):
        assert V.VARIABLES_BY_KEY[key]["purposes"] == ["recruiter_batch", "team_digest", "generic"]
    assert "check_action" in V.allowed_keys("recruiter_reminder")
    for name in ("clawback_daily_checklist", "clawback_team_checklist"):
        spec = next(t for t in seeds.DEFAULT_TEMPLATES if t["name"] == name)
        assert "{{candidate_list}}" in spec["body_text"] and spec["header_text"]


def test_seed_list_templates_pass_validation():
    from app.whatsapp_layer import templates_service as ts
    for spec in seeds.DEFAULT_TEMPLATES:
        data = {"name": spec["name"], "language": "en_US", "category": "UTILITY",
                "purpose": spec["purpose"],
                "header_format": "TEXT" if spec.get("header_text") else "NONE",
                "header_text": spec.get("header_text"), "body_text": spec["body_text"],
                "footer_text": None, "buttons": [],
                "sample_values": {k: V.sample_for(k) for k in ts.extract_keys(spec["body_text"])}}
        assert ts.validate_template(data, for_submit=True) == [], spec["name"]


def test_plan_combine_flag():
    es = [{"recruiter_user_id": 7, "kind": "reminder", "item": {"candidate_job_id": i}}
          for i in range(2)]
    assert [m.kind for m in E.plan_recruiter_messages(es, 0)] == ["batch_reminder"]
    assert [m.kind for m in E.plan_recruiter_messages(es, 0, combine=False)] == ["reminder"] * 2
    assert [m.kind for m in E.plan_recruiter_messages(es[:1], 1)] == ["reminder"]


# ---------------------------------------------------------------------------
# Pure: schedule summaries / descriptions
# ---------------------------------------------------------------------------
def test_schedule_summaries():
    s = EV.rule_schedule_summary
    assert s("every_n_days", {"n": 2}, "10:00") == "Every 2 days · 10:00"
    assert s("specific_days", {"days": [1, 3, 7]}, "10:00") == "Days 1, 3, 7 · 10:00"
    assert s("weekdays", {"weekdays": ["THU", "MON"]}, "10:00") == "Mon, Thu · 10:00"
    assert s("weekly", {}, "10:00") == "Weekly · 10:00"
    assert s("once", {}, "10:00") == "Once · 10:00"
    d = EV.digest_schedule_summary
    assert d("daily", {}, "09:30") == "Daily · 09:30"
    assert d("weekly", {"weekday": "MON"}, "09:30") == "Weekly on Mon · 09:30"


def test_rule_descriptions():
    assert EV.rule_description("doj", 8, 30, "weekly", {}) == \
        "Health-check reminder to the recruiter, weekly from day 8 to day 30"
    assert EV.rule_description("offer_accepted", 1, None, "every_n_days", {"n": 2}) == \
        "Health-check reminder to the recruiter, every 2 days from offer acceptance until joining"
    assert EV.rule_description("doj", 1, 7, "specific_days", {"days": [1, 3, 7]}) == \
        "Health-check reminder to the recruiter, on days 1, 3 and 7 after joining"
    assert "on the day the cooling period ends" in EV.rule_description("cooling_end", 0, 0, "once", {})


# ---------------------------------------------------------------------------
# DB: engine
# ---------------------------------------------------------------------------
def _item(cj, rec, joined_days_ago=7, name=None):
    joined = TODAY - timedelta(days=joined_days_ago)
    return {"candidate_job_id": cj, "candidate_job_status_id": cj + 1000, "status": "in_cooling",
            "organization_id": ORG, "candidate_id": f"C{cj}", "candidate_name": name or f"Cand {cj}",
            "job_id": 9, "job_title": "Sales Executive", "company_name": "Acme",
            "offer_accepted_at": None, "joined_at": joined.isoformat(), "cooling_period_days": 90,
            "clawback_completion_date": (joined + timedelta(days=90)).isoformat(),
            "cooling_period_closed": None, "recruiter_user_id": rec, "assigned_to": rec,
            "created_by": 3}


def _pre_joining(cj, rec):
    it = _item(cj, rec, name=f"Pre {cj}")
    it.update({"status": "pre_joining", "joined_at": None, "clawback_completion_date": None,
               "offer_accepted_at": (TODAY - timedelta(days=2)).isoformat()})   # day 3 → due
    return it


@pytest.fixture
def org(wa_db, monkeypatch):
    db = wa_db
    monkeypatch.setattr("app.whatsapp_layer.people.get_users", lambda _db, ids: {
        int(i): {"id": int(i), "name": f"User {i}", "email": f"u{i}@x", "organization_id": ORG,
                 "phone_e164": PHONES.get(int(i)), "enabled": True} for i in ids if i})
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


def test_default_threshold_is_zero(org):
    assert E.get_settings(org, ORG).recruiter_batch_threshold == 0
    assert EV.system_enabled(E.get_settings(org, ORG), EV.RECRUITER_SUMMARY)


def test_one_due_still_gets_the_list(org):
    E.evaluate_org_reminders(org, ORG, [_item(1, 7)], NOW)
    log = org.query(L).one()
    assert log.kind == "batch_reminder" and log.template_name == "clawback_daily_checklist"


def test_two_due_one_list_message(org):
    E.evaluate_org_reminders(org, ORG, [_item(1, 8, name="Ravi\nK"), _pre_joining(2, 8)], NOW)
    log = org.query(L).one()
    assert log.kind == "batch_reminder" and log.template_name == "clawback_daily_checklist"
    checks = {c.candidate_job_id: c.id for c in org.query(H).all()}
    assert len(checks) == 2 and set(log.health_check_ids) == set(checks.values())
    assert log.params["candidate_count"] == "2" and log.params["due_count"] == "2"
    assert log.params["part_label"] == "2 candidates"
    assert log.params["candidate_list"] == (
        f"1) Ravi K – Acme – joined 19 Oct, day 8 of 90 – Weekly check-in call – {LINK}{checks[1]} · "
        f"2) Pre 2 – Acme – offer accepted 24 Oct, not joined yet – Call to confirm joining – "
        f"{LINK}{checks[2]}")
    assert len(log.params["candidates"]) == 2
    assert log.rendered_body.startswith(
        "Hi User 8, here are your clawback check-ins for today (2 candidates):\n\n1) Ravi K – Acme")
    assert "Still pending from earlier days: 0." in log.rendered_body


def test_recruiter_summary_off_sends_individual_messages(org):
    st = E.get_settings(org, ORG)
    EV.set_system_toggle(st, EV.RECRUITER_SUMMARY, False, 1)
    org.commit()
    E.evaluate_org_reminders(org, ORG, [_item(1, 8), _item(2, 8)], NOW)
    logs = org.query(L).order_by(L.id).all()
    assert [l.kind for l in logs] == ["reminder", "reminder"]
    assert {l.template_name for l in logs} == {"clawback_health_check_due"}


# ---------------------------------------------------------------------------
# DB: events API
# ---------------------------------------------------------------------------
def _ctx():
    return AuthCtx(user_id=1, organization_id=ORG, role_id=1, role_name="admin", role_type="admin",
                   permissions=set(), is_super_admin=False, is_admin=True, token="t")


def test_events_listing(org):
    data = events_api.list_events(None, _ctx(), org)["data"]
    groups = [e["group"] for e in data]
    order = [g for i, g in enumerate(groups) if i == 0 or groups[i - 1] != g]
    assert order == EV.GROUPS
    rules = {r.name: r.id for r in org.query(R).all()}
    reminders = [e for e in data if e["group"] == "recruiter_reminders"]
    assert [e["label"] for e in reminders] == [
        "Offer accepted, pre-joining", "DOJ to Day 7", "Days 8–30", "Days 31–60",
        "Days 61–75", "Days 76–90"]
    assert reminders[2]["key"] == f"rule:{rules['Days 8–30']}"
    assert reminders[2]["description"] == \
        "Health-check reminder to the recruiter, weekly from day 8 to day 30"
    assert reminders[2]["schedule_summary"] == "Weekly · 10:00"
    assert [e["schedule_summary"] for e in reminders] == [
        "Every 2 days · 10:00", "Days 1, 3, 7 · 10:00", "Weekly · 10:00", "Weekly · 10:00",
        "Mon, Thu · 10:00", "Every 2 days · 10:00"]
    assert reminders[0]["template"]["name"] == "clawback_health_check_due"
    final = [e for e in data if e["group"] == "final_validation"]
    assert [e["key"] for e in final] == [f"rule:{rules['Cooling completed']}"]
    assert final[0]["schedule_summary"] == "Once · 10:00"
    digests = [e for e in data if e["group"] == "team_summaries" and e["type"] != "system"]
    assert [e["key"] for e in data if e["group"] == "team_summaries" and e["type"] == "system"] == [
        "system:eod_report"]
    assert {e["schedule_summary"] for e in digests} == {"Daily · 09:30"}
    assert all(e["key"].startswith("digest:") for e in digests)
    system = [e for e in data if e["type"] == "system"]
    assert [e["key"] for e in system] == [
        "system:recruiter_summary", "system:eod_report", "system:alert_failure_rate",
        "system:alert_template_rejected", "system:alert_account_error"]
    assert all(e["enabled"] for e in system)
    summary = system[0]
    assert summary["template"]["name"] == "clawback_daily_checklist"
    assert summary["recipients"] == "Recruiter"
    assert all(e["recipients"] == "Org admins (in-app)" for e in system[2:])
    assert all(set(e) == {"key", "type", "ref_id", "group", "label", "description", "enabled",
                          "template", "schedule_summary", "recipients"} for e in data)


def test_patch_rule_and_digest(org):
    rule = org.query(R).filter(R.name == "Days 8–30").one()
    ev = events_api.toggle_event(f"rule:{rule.id}", EventToggle(enabled=False), None, _ctx(), org)
    assert ev["enabled"] is False and ev["key"] == f"rule:{rule.id}"
    org.refresh(rule)
    assert rule.is_active == 0
    ev = events_api.toggle_event(f"rule:{rule.id}", EventToggle(enabled=True), None, _ctx(), org)
    assert ev["enabled"] is True

    dg = org.query(D).order_by(D.id).first()
    ev = events_api.toggle_event(f"digest:{dg.id}", EventToggle(enabled=False), None, _ctx(), org)
    assert ev["enabled"] is False and ev["group"] == "team_summaries"
    org.refresh(dg)
    assert dg.is_active == 0


def test_patch_system_toggles(org):
    ev = events_api.toggle_event("system:recruiter_summary", EventToggle(enabled=False), None,
                                 _ctx(), org)
    assert ev["enabled"] is False and ev["group"] == "recruiter_summary"
    ev = events_api.toggle_event("system:alert_failure_rate", EventToggle(enabled=False), None,
                                 _ctx(), org)
    assert ev["enabled"] is False and ev["group"] == "admin_alerts"
    st = E.get_settings(org, ORG)
    assert st.event_toggles == {"recruiter_summary": False, "alert_failure_rate": False}
    listed = {e["key"]: e["enabled"] for e in events_api.list_events(None, _ctx(), org)["data"]}
    assert listed["system:recruiter_summary"] is False
    assert listed["system:alert_template_rejected"] is True


@pytest.mark.parametrize("key", ["system:nope", "bogus:1", "rule:abc", "rule:99999", "digest:99999", ""])
def test_patch_unknown_key_404(org, key):
    with pytest.raises(HTTPException) as exc:
        events_api.toggle_event(key, EventToggle(enabled=False), None, _ctx(), org)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# DB: alerts respect their toggles
# ---------------------------------------------------------------------------
@pytest.fixture
def admin_lookups(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "_first_time", lambda *a, **k: True)
    monkeypatch.setattr(alerts, "org_admin_user_ids",
                        lambda _db, org_id: calls.append(org_id) or [])
    return calls


def _toggle(db, name, enabled):
    st = E.get_settings(db, ORG)
    EV.set_system_toggle(st, name, enabled, 1)
    db.commit()


def test_notify_org_admins_suppressed_when_off(org, admin_lookups):
    _toggle(org, EV.ALERT_FAILURE_RATE, False)
    assert alerts.notify_org_admins(org, ORG, title="t", message="m",
                                    event=EV.ALERT_FAILURE_RATE) is None
    assert admin_lookups == []
    # Other alerts (and untagged calls) still go through.
    alerts.notify_org_admins(org, ORG, title="t", message="m", event=EV.ALERT_TEMPLATE_REJECTED)
    alerts.notify_org_admins(org, ORG, title="t", message="m")
    assert admin_lookups == [ORG, ORG]


def test_hourly_alerts_respect_toggles(org, admin_lookups):
    org.query(A).update({A.status: "auth_error"})
    tpl = org.query(T).first()
    tpl.approval_status, tpl.rejection_reason = "rejected", "bad"
    org.commit()
    _toggle(org, EV.ALERT_ACCOUNT_ERROR, False)
    _toggle(org, EV.ALERT_TEMPLATE_REJECTED, False)
    tasks.check_failure_alerts(org)
    assert admin_lookups == []
    _toggle(org, EV.ALERT_ACCOUNT_ERROR, True)
    tasks.check_failure_alerts(org)
    assert admin_lookups == [ORG]


def test_auth_error_alert_respects_toggle(org, admin_lookups):
    _toggle(org, EV.ALERT_ACCOUNT_ERROR, False)
    acct = org.query(A).one()
    accounts.mark_auth_error(org, acct, "401")
    assert acct.status == "auth_error" and admin_lookups == []
