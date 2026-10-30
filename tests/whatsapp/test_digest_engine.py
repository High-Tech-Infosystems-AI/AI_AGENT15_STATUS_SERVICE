"""Digest engine pure functions: priority de-dup, schedule, aggregation."""
from datetime import date, timedelta

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import digest_engine as D
from app.whatsapp_layer import seeds

MONDAY = date(2026, 10, 26)


def _rules():
    return [D.as_digest_spec({**r, "id": i + 1, "is_active": True})
            for i, r in enumerate(seeds.DEFAULT_DIGEST_RULES)]


def test_digest_priority_dedup_one_per_person():
    tl, senior, admins = _rules()   # priority 10, 20, 30 — all daily
    tl_users = [{"user_id": 9, "subordinate_user_ids": [7]}, {"user_id": 20, "subordinate_user_ids": [9, 7]},
                {"user_id": 1, "subordinate_user_ids": [5]}]
    senior_users = [{"user_id": 20, "subordinate_user_ids": [9, 7, 11]}]
    admin_users = [{"user_id": 1, "subordinate_user_ids": []}]
    out = D.assign_recipients([(tl, tl_users), (senior, senior_users), (admins, admin_users)])
    assert set(out) == {1, 9, 20}
    assert out[1][0].name == "Admins – daily" and out[1][0].scope == "organisation"
    assert out[20][0].name == "Senior managers – daily"
    assert out[20][1]["subordinate_user_ids"] == [9, 7, 11]
    assert out[9][0].name == "Team leads – daily"


def test_digest_priority_tie_lowest_id_wins():
    a = D.as_digest_spec({"id": 5, "name": "a", "audience_type": "users", "priority": 1})
    b = D.as_digest_spec({"id": 3, "name": "b", "audience_type": "users", "priority": 1})
    out = D.assign_recipients([(a, [{"user_id": 1}]), (b, [{"user_id": 1}])])
    assert out[1][0].name == "b"


def test_digest_schedule():
    tl, senior, admins = _rules()
    for r in (tl, senior, admins):
        assert D.is_digest_due(r, MONDAY) and D.is_digest_due(r, MONDAY + timedelta(days=1))
    weekly = D.as_digest_spec({"id": 9, "name": "w", "audience_type": "users", "frequency_type": "weekly",
                               "frequency_value": {"weekday": "MON"}})
    assert D.is_digest_due(weekly, MONDAY) and not D.is_digest_due(weekly, MONDAY + timedelta(days=1))
    wd = D.as_digest_spec({"id": 1, "name": "x", "audience_type": "users",
                           "frequency_type": "weekdays", "frequency_value": {"weekdays": ["MON", "THU"]}})
    assert D.is_digest_due(wd, MONDAY + timedelta(days=3)) and not D.is_digest_due(wd, MONDAY + timedelta(days=2))


def test_digest_period():
    tl = _rules()[0]
    assert D.digest_period(tl, MONDAY) == (MONDAY, "today")
    weekly = D.as_digest_spec({"id": 9, "name": "w", "audience_type": "users", "frequency_type": "weekly",
                               "frequency_value": {"weekday": "MON"}})
    assert D.digest_period(weekly, MONDAY) == (MONDAY - timedelta(days=6), "last 7 days")
    wd = D.as_digest_spec({"id": 1, "name": "x", "audience_type": "users",
                           "frequency_type": "weekdays", "frequency_value": {"weekdays": ["MON", "THU"]}})
    start, _ = D.digest_period(wd, MONDAY)   # previous scheduled day = THU → period FRI..MON
    assert start == MONDAY - timedelta(days=3)


def test_audience_request_mapping():
    mk = lambda at, av: D.as_digest_spec({"id": 1, "name": "x", "audience_type": at, "audience_value": av})
    assert D.audience_request(mk("team_managers", {})) == {"audience": "team_managers"}
    assert D.audience_request(mk("designation_levels", {"min_level": 2, "max_level": 3})) == \
        {"audience": "all_managers", "min_level": 2, "max_level": 3}
    assert D.audience_request(mk("roles", {"role_ids": [4]})) == {"audience": "all_managers", "role_ids": [4]}
    assert D.audience_request(mk("users", {"user_ids": [7, 9]})) == {"audience": "users", "user_ids": [7, 9]}
    assert D.audience_request(mk("admins", {})) == {"audience": "admins"}


def test_validate_digest_fields():
    ok = {"audience_type": "team_managers", "scope": "full_subtree", "frequency_type": "weekly",
          "frequency_value": {"weekday": "MON"}, "send_time": "09:30"}
    assert D.validate_digest_fields(ok) == []
    bad = {**ok, "audience_type": "users", "audience_value": {}, "scope": "x",
           "frequency_value": {"weekday": "XYZ"}}
    assert {"audience_value", "scope", "frequency_value"} <= {e["field"] for e in D.validate_digest_fields(bad)}


def _item(cj, rec, joined, cp=90, status="in_cooling"):
    return {"candidate_job_id": cj, "recruiter_user_id": rec, "assigned_to": rec, "status": status,
            "joined_at": joined.isoformat() if joined else None, "cooling_period_days": cp,
            "offer_accepted_at": "2026-01-01", "clawback_completion_date": None}


def test_aggregate_scope_and_counts():
    today = MONDAY
    items = [
        _item(1, 7, today - timedelta(days=10)),            # Days 8–30
        _item(2, 7, today - timedelta(days=86)),            # ending within 7 days
        _item(3, 11, today - timedelta(days=95)),           # cooling ended → final pending
        _item(4, 99, today - timedelta(days=10)),           # out of scope
        _item(5, 7, None, status="pre_joining"),            # pre-joining
    ]
    checks = [
        {"id": 1, "recruiter_user_id": 7, "due_date": today, "status": "done", "outcome": "engaged"},
        {"id": 2, "recruiter_user_id": 7, "due_date": today, "status": "done", "outcome": "at_risk"},
        {"id": 3, "recruiter_user_id": 11, "due_date": today - timedelta(days=1), "status": "missed", "outcome": None},
        {"id": 4, "recruiter_user_id": 11, "due_date": today, "status": "pending", "outcome": None},
        {"id": 5, "recruiter_user_id": 7, "due_date": today, "status": "cancelled", "outcome": None},
        {"id": 6, "recruiter_user_id": 99, "due_date": today, "status": "missed", "outcome": None},
    ]
    rules = [E.as_rule_spec({**r, "id": i + 1}) for i, r in enumerate(seeds.DEFAULT_RULES)]
    agg = D.aggregate({7, 11}, items, checks, rules, today, today - timedelta(days=6))
    t = agg["totals"]
    assert t["active"] == 4 and t["ending_soon"] == 1 and t["final_pending"] == 1
    assert t["due"] == 4 and t["done"] == 2 and t["missed"] == 1 and t["at_risk"] == 1
    assert agg["recruiters"][11] == {"due": 2, "done": 0, "missed": 1}
    assert agg["top_missed_recruiter_ids"] == [11]
    assert agg["at_risk_check_ids"] == [2]
    stages = {s["stage_label"]: s["count"] for s in agg["by_stage"]}
    assert stages["Days 8–30"] == 1 and stages["Offer accepted, pre-joining"] == 1
    # Daily period only counts today's checks.
    assert D.aggregate({7, 11}, items, checks, rules, today, today)["totals"]["missed"] == 0
    # None = whole org.
    assert D.aggregate(None, items, checks, rules, today, today)["totals"]["active"] == 5


def test_digest_params_and_empty():
    totals = {"active": 4, "due": 4, "done": 2, "missed": 1, "at_risk": 1, "not_reachable": 1,
              "dropped": 0, "ending_soon": 1, "final_pending": 1}
    p = D.digest_params(totals, "Meera", D.scope_label(12), "today", "https://x/clawback/team-health")
    assert p["at_risk_count"] == "2" and p["scope_label"] == "Your team (12 recruiters)"
    assert D.is_empty({k: 0 for k in totals}) and not D.is_empty(totals)
