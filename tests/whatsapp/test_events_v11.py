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
# candidate_list formatting (pure)
# ---------------------------------------------------------------------------
def _entry(n, name=None, **kw):
    return V.candidate_list_entry(n, candidate_name=name or f"Cand {n}", job_title="Sales Executive",
                                  company_name="Acme", day_number=14, cooling_period_days=90, **kw)


def test_candidate_list_one_entry():
    value, shown = V.format_candidate_list([_entry(1, "Ravi K")])
    assert value == "1) Ravi K – Sales Executive, Acme (day 14 of 90)"
    assert shown == 1


def test_candidate_list_two_entries():
    value, _ = V.format_candidate_list([_entry(1, "Ravi K"), _entry(2, "Neha P")])
    assert value == ("1) Ravi K – Sales Executive, Acme (day 14 of 90) · "
                     "2) Neha P – Sales Executive, Acme (day 14 of 90)")


def test_candidate_list_eight_entries_no_more_suffix():
    value, shown = V.format_candidate_list([_entry(i) for i in range(1, 9)])
    assert shown == 8 and "more" not in value
    assert value.count(" · ") == 7 and value.endswith("8) Cand 8 – Sales Executive, Acme (day 14 of 90)")


def test_candidate_list_twelve_entries_capped_at_eight():
    value, shown = V.format_candidate_list([_entry(i) for i in range(1, 13)])
    assert shown == 8
    assert value.endswith("8) Cand 8 – Sales Executive, Acme (day 14 of 90) · +4 more")
    assert "9) Cand 9" not in value
    assert len(value) <= 600


def test_candidate_list_600_char_cap_adjusts_more_count():
    long_name = "Candidate With A Rather Long Name " * 2         # ~68 chars
    entries = [_entry(i, f"{long_name}{i}") for i in range(1, 13)]
    value, shown = V.format_candidate_list(entries)
    assert len(value) <= 600
    assert shown < 8
    assert value.endswith(f" · +{12 - shown} more")
    # Adding the next entry back would break the cap.
    longer = " · ".join(entries[:shown + 1]) + f" · +{12 - shown - 1} more"
    assert len(longer) > 600


def test_candidate_list_single_huge_entry_is_cut():
    value, shown = V.format_candidate_list([_entry(1, "X" * 900), _entry(2)])
    assert len(value) <= 600 and shown == 1
    assert value.endswith("… · +1 more")


def test_candidate_list_sanitises_names():
    e = V.candidate_list_entry(1, candidate_name="Ravi\nK\t  Kumar     X\x07",
                               job_title="Sales\r\nExec", company_name="  Acme    Ltd ",
                               day_number=3, cooling_period_days=90)
    value, _ = V.format_candidate_list([e])
    assert value == "1) Ravi K Kumar X – Sales Exec, Acme Ltd (day 3 of 90)"
    assert "\n" not in value and "\t" not in value and "  " not in value


def test_candidate_list_pre_joining_entry():
    e = V.candidate_list_entry(2, candidate_name="Neha P", job_title="Store Manager",
                               company_name="Acme", pre_joining=True,
                               offer_accepted_date=date(2026, 10, 20))
    assert e == "2) Neha P – Store Manager, Acme (offer accepted 20 Oct)"
    assert V.short_date(date(2026, 1, 5)) == "5 Jan"


def test_candidate_list_params_keeps_full_list():
    entries = [{"item": {"candidate_job_id": i, "candidate_name": f"Cand {i}",
                         "job_title": "SE", "company_name": "Acme", "cooling_period_days": 90},
                "anchor": "doj", "day": 10, "check": None} for i in range(1, 13)]
    p = E.candidate_list_params(entries)
    assert p["candidate_count"] == "12"
    assert p["candidate_list"].endswith("+4 more")
    assert len(p["candidates"]) == 12 and p["candidates"][11]["candidate_job_id"] == 12
    assert p["candidates"][11]["entry"] == "12) Cand 12 – SE, Acme (day 10 of 90)"


def test_catalogue_has_new_batch_variables():
    for key in ("candidate_count", "candidate_list"):
        assert V.VARIABLES_BY_KEY[key]["purposes"] == ["recruiter_batch", "generic"]
    batch = next(t for t in seeds.DEFAULT_TEMPLATES if t["name"] == "clawback_health_checks_batch")
    assert batch["body_text"] == (
        "Hi {{recruiter_name}}, you have {{candidate_count}} clawback health checks due today: "
        "{{candidate_list}}. Also pending from earlier days: {{overdue_count}}. Please record "
        "each outcome here: {{app_link}}.")


def test_plan_combine_flag():
    es = [{"recruiter_user_id": 7, "kind": "reminder", "item": {"candidate_job_id": i}}
          for i in range(2)]
    assert [m.kind for m in E.plan_recruiter_messages(es, 1)] == ["batch_reminder"]
    assert [m.kind for m in E.plan_recruiter_messages(es, 1, combine=False)] == ["reminder"] * 2
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
        tpl.approval_status = "approved"
    db.commit()
    return db


def test_default_threshold_is_one(org):
    assert E.get_settings(org, ORG).recruiter_batch_threshold == 1
    assert EV.system_enabled(E.get_settings(org, ORG), EV.RECRUITER_SUMMARY)


def test_one_due_uses_rule_template(org):
    E.evaluate_org_reminders(org, ORG, [_item(1, 7)], NOW)
    log = org.query(L).one()
    assert log.kind == "reminder" and log.template_name == "clawback_health_check_due"


def test_two_due_one_batch_message(org):
    E.evaluate_org_reminders(org, ORG, [_item(1, 8, name="Ravi\nK"), _pre_joining(2, 8)], NOW)
    log = org.query(L).one()
    assert log.kind == "batch_reminder" and log.template_name == "clawback_health_checks_batch"
    check_ids = {c.id for c in org.query(H).all()}
    assert len(check_ids) == 2 and set(log.health_check_ids) == check_ids
    assert log.params["candidate_count"] == "2" and log.params["due_count"] == "2"
    assert log.params["candidate_list"] == (
        "1) Ravi K – Sales Executive, Acme (day 8 of 90) · "
        "2) Pre 2 – Sales Executive, Acme (offer accepted 24 Oct)")
    assert len(log.params["candidates"]) == 2
    assert log.rendered_body.startswith(
        "Hi User 8, you have 2 clawback health checks due today: 1) Ravi K – Sales Executive")
    assert "Also pending from earlier days: 0." in log.rendered_body


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
    digests = [e for e in data if e["group"] == "team_summaries"]
    assert {e["schedule_summary"] for e in digests} == {"Daily · 09:30", "Weekly on Mon · 09:30"}
    assert all(e["key"].startswith("digest:") for e in digests)
    system = [e for e in data if e["type"] == "system"]
    assert [e["key"] for e in system] == [
        "system:recruiter_summary", "system:alert_failure_rate",
        "system:alert_template_rejected", "system:alert_account_error"]
    assert all(e["enabled"] for e in system)
    summary = system[0]
    assert summary["template"]["name"] == "clawback_health_checks_batch"
    assert summary["recipients"] == "Recruiter"
    assert all(e["recipients"] == "Org admins (in-app)" for e in system[1:])
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
