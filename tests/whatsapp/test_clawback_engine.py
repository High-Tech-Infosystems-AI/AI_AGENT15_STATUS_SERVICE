"""Pure engine functions: day numbers, "is due", window boundaries, overlap,
quiet hours, idempotency key, batching."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.whatsapp_layer import clawback_engine as E
from app.whatsapp_layer import seeds

JOINED = date(2026, 1, 1)          # Thursday
COMPLETION = date(2026, 4, 1)      # joined + 90 days


def _item(**kw):
    base = {"candidate_job_id": 44, "candidate_job_status_id": 12, "status": "in_cooling",
            "organization_id": 12, "candidate_id": "CAND123", "candidate_name": "Ravi K",
            "job_id": 9, "job_title": "Sales Executive", "company_name": "Acme",
            "offer_accepted_at": "2025-12-10", "joined_at": JOINED.isoformat(),
            "cooling_period_days": 90, "clawback_completion_date": COMPLETION.isoformat(),
            "cooling_period_closed": None, "recruiter_user_id": 7, "assigned_to": 7,
            "created_by": 3}
    base.update(kw)
    return base


def _seed_rules():
    return [E.as_rule_spec({**r, "id": i + 1, "send_time": "10:00", "is_active": True,
                            "priority": 0, "stage_label": r["name"]})
            for i, r in enumerate(seeds.DEFAULT_RULES)]


def _day_date(day):
    """Calendar date of DOJ day N (DOJ = Day 1)."""
    return JOINED + timedelta(days=day - 1)


# ---------------------------------------------------------------------------
# Day numbers
# ---------------------------------------------------------------------------
def test_doj_is_day_one():
    assert E.anchor_and_day(_item(), JOINED) == ("doj", 1)
    assert E.anchor_and_day(_item(), JOINED + timedelta(days=6)) == ("doj", 7)


def test_day_90_is_last_doj_day_and_completion_is_cooling_end_day_zero():
    assert E.anchor_and_day(_item(), _day_date(90)) == ("doj", 90)
    assert _day_date(91) == COMPLETION
    assert E.anchor_and_day(_item(), COMPLETION) == ("cooling_end", 0)
    assert E.anchor_and_day(_item(), COMPLETION + timedelta(days=3)) == ("cooling_end", 3)


def test_completion_date_computed_when_missing():
    item = _item(clawback_completion_date=None)
    assert E.completion_date_of(item) == COMPLETION
    assert E.anchor_and_day(item, COMPLETION) == ("cooling_end", 0)


def test_shorter_cooling_period_reaches_cooling_end_earlier():
    item = _item(cooling_period_days=60, clawback_completion_date=None)
    assert E.anchor_and_day(item, _day_date(60)) == ("doj", 60)
    assert E.anchor_and_day(item, _day_date(61)) == ("cooling_end", 0)


def test_pre_joining_counts_from_offer_accepted():
    item = _item(status="pre_joining", joined_at=None, clawback_completion_date=None,
                 candidate_job_status_id=None, offer_accepted_at="2026-09-20")
    assert E.anchor_and_day(item, date(2026, 9, 20)) == ("offer_accepted", 1)
    assert E.anchor_and_day(item, date(2026, 9, 22)) == ("offer_accepted", 3)
    assert E.anchor_and_day(item, date(2026, 9, 19)) == (None, None)


def test_closed_clawback_has_no_anchor():
    assert E.anchor_and_day(_item(cooling_period_closed="2026-02-01"), date(2026, 2, 2)) == (None, None)


def test_day_number_formula():
    assert E.day_number("doj", date(2026, 1, 8), JOINED) == 8
    assert E.day_number("offer_accepted", JOINED, JOINED) == 1
    assert E.day_number("cooling_end", COMPLETION, COMPLETION) == 0


# ---------------------------------------------------------------------------
# is_rule_due per frequency type
# ---------------------------------------------------------------------------
def _rule(**kw):
    base = {"id": 1, "name": "r", "anchor": "doj", "start_day": 8, "end_day": 30,
            "frequency_type": "weekly", "frequency_value": {}}
    base.update(kw)
    return E.as_rule_spec(base)


def test_every_n_days():
    r = _rule(frequency_type="every_n_days", frequency_value={"n": 2}, start_day=76, end_day=90)
    assert [d for d in range(70, 95) if E.is_rule_due(r, d, "MON")] == list(range(76, 91, 2))


def test_specific_days():
    r = _rule(frequency_type="specific_days", frequency_value={"days": [1, 3, 7]}, start_day=1, end_day=7)
    assert [d for d in range(0, 12) if E.is_rule_due(r, d, "MON")] == [1, 3, 7]


def test_weekly():
    r = _rule(frequency_type="weekly", start_day=8, end_day=30)
    assert [d for d in range(1, 40) if E.is_rule_due(r, d, "MON")] == [8, 15, 22, 29]


def test_weekdays():
    r = _rule(frequency_type="weekdays", frequency_value={"weekdays": ["MON", "THU"]},
              start_day=61, end_day=75)
    assert E.is_rule_due(r, 61, "MON") and E.is_rule_due(r, 64, "THU")
    assert not E.is_rule_due(r, 62, "TUE")
    assert not E.is_rule_due(r, 60, "MON") and not E.is_rule_due(r, 76, "THU")


def test_once():
    r = _rule(frequency_type="once", anchor="cooling_end", start_day=0, end_day=0)
    assert E.is_rule_due(r, 0, "WED")
    assert not E.is_rule_due(r, 1, "WED")


def test_open_ended_window():
    r = _rule(frequency_type="every_n_days", frequency_value={"n": 2}, anchor="offer_accepted",
              start_day=1, end_day=None)
    assert E.is_rule_due(r, 1, "MON") and E.is_rule_due(r, 201, "MON")
    assert not E.is_rule_due(r, 2, "MON")


# ---------------------------------------------------------------------------
# Seeded matrix at every window boundary (90-day job)
# ---------------------------------------------------------------------------
def _fires(day):
    d = _day_date(day)
    anchor, n = E.anchor_and_day(_item(), d)
    rule = E.match_rule(_seed_rules(), anchor, n, E.weekday_code(d))
    return rule.name if rule else None


@pytest.mark.parametrize("day,expected", [
    (1, "DOJ to Day 7"), (2, None), (3, "DOJ to Day 7"), (7, "DOJ to Day 7"),
    (8, "Days 8–30"), (9, None), (29, "Days 8–30"), (30, None),
    (31, "Days 31–60"), (38, "Days 31–60"), (59, "Days 31–60"), (60, None),
    (61, "Days 61–75"),          # 2026-03-02 is a Monday
    (62, None), (64, "Days 61–75"),  # Thursday
    (74, None), (75, "Days 61–75"),  # 2026-03-16 is a Monday (last day of the window)
    (76, "Days 76–90"), (77, None), (88, "Days 76–90"), (90, "Days 76–90"),
    (91, "Cooling completed"),   # completion date = cooling_end Day 0
    (92, None),
])
def test_seed_matrix_boundaries(day, expected):
    assert _fires(day) == expected


def test_seed_matrix_full_schedule():
    events = E.project_schedule(_seed_rules(), _item(), JOINED, 100)
    days = [(e["anchor"], e["day_number"]) for e in events]
    doj_days = [d for a, d in days if a == "doj"]
    assert doj_days[:6] == [1, 3, 7, 8, 15, 22]
    assert 30 not in doj_days and 31 in doj_days and 60 not in doj_days
    assert all(d <= 90 for d in doj_days)
    assert days.count(("cooling_end", 0)) == 1


def test_sixty_day_job_stops_after_days_31_60_then_final_validation():
    item = _item(cooling_period_days=60, clawback_completion_date=None)
    events = E.project_schedule(_seed_rules(), item, JOINED, 100)
    assert max(e["day_number"] for e in events if e["anchor"] == "doj") <= 60
    finals = [e for e in events if e["anchor"] == "cooling_end"]
    assert len(finals) == 1 and finals[0]["date"] == JOINED + timedelta(days=60)


def test_next_reminder_date():
    assert E.next_reminder_date(_seed_rules(), _item(), _day_date(4)) == _day_date(7)
    assert E.next_reminder_date(_seed_rules(), _item(), _day_date(7)) == _day_date(7)


def test_match_rule_priority_wins():
    a = _rule(id=1, name="a", priority=0, frequency_type="weekly")
    b = _rule(id=2, name="b", priority=5, frequency_type="every_n_days", frequency_value={"n": 1})
    assert E.match_rule([a, b], "doj", 8, "MON").name == "b"
    b.is_active = False
    assert E.match_rule([a, b], "doj", 8, "MON").name == "a"


# ---------------------------------------------------------------------------
# Overlap validation
# ---------------------------------------------------------------------------
def test_seeds_do_not_overlap():
    rules = _seed_rules()
    for r in rules:
        assert E.find_overlap(r, rules) is None


def test_overlap_detected_same_anchor():
    existing = [_rule(id=3, name="Days 8–30", start_day=8, end_day=30)]
    clash = E.find_overlap(_rule(id=None, name="new", start_day=30, end_day=40), existing)
    assert clash is not None and clash.name == "Days 8–30"


def test_adjacent_windows_do_not_overlap():
    existing = [_rule(id=3, start_day=8, end_day=30)]
    assert E.find_overlap(_rule(id=None, start_day=31, end_day=60), existing) is None
    assert E.find_overlap(_rule(id=None, start_day=1, end_day=7), existing) is None


def test_overlap_ignores_other_anchor_inactive_and_self():
    existing = [_rule(id=3, start_day=8, end_day=30)]
    assert E.find_overlap(_rule(id=None, anchor="offer_accepted", start_day=1, end_day=None), existing) is None
    assert E.find_overlap(_rule(id=None, start_day=10, end_day=12, is_active=False), existing) is None
    assert E.find_overlap(_rule(id=3, start_day=9, end_day=31), existing) is None
    inactive = [_rule(id=4, start_day=8, end_day=30, is_active=False)]
    assert E.find_overlap(_rule(id=None, start_day=10, end_day=12), inactive) is None


def test_open_ended_overlaps_everything_after():
    existing = [_rule(id=1, anchor="offer_accepted", start_day=1, end_day=None)]
    assert E.find_overlap(_rule(id=None, anchor="offer_accepted", start_day=500, end_day=501), existing)


def test_validate_rule_fields():
    ok = {"anchor": "doj", "start_day": 8, "end_day": 30, "frequency_type": "weekly",
          "frequency_value": {}, "send_time": "10:00"}
    assert E.validate_rule_fields(ok) == []
    fields = {e["field"] for e in E.validate_rule_fields(
        {**ok, "anchor": "x", "end_day": 2, "frequency_type": "every_n_days",
         "frequency_value": {"n": 0}, "send_time": "25:99"})}
    assert {"anchor", "end_day", "frequency_value", "send_time"} <= fields
    assert E.validate_rule_fields({**ok, "anchor": "cooling_end", "start_day": 0, "end_day": 0}) == []


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------
def test_quiet_hours_wrapping_midnight():
    assert E.in_quiet_hours(time(22, 0), "21:00", "08:00")
    assert E.in_quiet_hours(time(7, 59), "21:00", "08:00")
    assert not E.in_quiet_hours(time(8, 0), "21:00", "08:00")
    assert not E.in_quiet_hours(time(10, 0), "21:00", "08:00")
    assert E.in_quiet_hours(time(13, 0), "12:00", "14:00")
    assert not E.in_quiet_hours(time(13, 0), None, None)


def test_working_days():
    assert E.is_working_day(date(2026, 10, 26), ["MON"])        # Monday
    assert not E.is_working_day(date(2026, 10, 25), E.DEFAULT_WORKING_DAYS)  # Sunday


def test_local_time_and_send_time():
    now_utc = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
    local = E.local_now("Asia/Kolkata", now_utc)
    assert local.hour == 10 and local.minute == 0
    assert E.time_reached(local, "10:00") and not E.time_reached(local, "10:01")
    assert E.local_to_utc_naive(date(2026, 10, 26), time(10, 0), "Asia/Kolkata") == datetime(2026, 10, 26, 4, 30)


# ---------------------------------------------------------------------------
# Idempotency + batching
# ---------------------------------------------------------------------------
def test_idempotency_key_is_stable_and_discriminating():
    k = E.idempotency_key("reminder", 3, 44, 7, date(2026, 10, 25))
    assert k == E.idempotency_key("reminder", 3, 44, 7, date(2026, 10, 25))
    assert len(k) == 40 and int(k, 16) >= 0  # sha1 hex digest
    others = {E.idempotency_key("reminder", 3, 44, 7, date(2026, 10, 26)),
              E.idempotency_key("batch_reminder", 3, 44, 7, date(2026, 10, 25)),
              E.idempotency_key("reminder", 4, 44, 7, date(2026, 10, 25)),
              E.idempotency_key("reminder", 3, 45, 7, date(2026, 10, 25)),
              E.idempotency_key("reminder", 3, 44, 8, date(2026, 10, 25))}
    assert k not in others and len(others) == 5
    assert E.idempotency_key("digest", None, None, 9, date(2026, 1, 1)) == \
        E.idempotency_key("digest", 0, 0, 9, date(2026, 1, 1))


def _entries(rec, n, kind="reminder"):
    return [{"recruiter_user_id": rec, "kind": kind, "item": {"candidate_job_id": rec * 100 + i}}
            for i in range(n)]


def test_batch_threshold_grouping():
    msgs = E.plan_recruiter_messages(_entries(7, 3) + _entries(8, 4), threshold=3)
    by_rec = {}
    for m in msgs:
        by_rec.setdefault(m.recruiter_user_id, []).append(m)
    assert [m.kind for m in by_rec[7]] == ["reminder"] * 3
    assert len(by_rec[8]) == 1 and by_rec[8][0].kind == "batch_reminder"
    assert len(by_rec[8][0].entries) == 4


def test_final_validation_joins_the_list():
    msgs = E.plan_recruiter_messages(_entries(7, 5) + _entries(7, 2, "final_validation"), threshold=0)
    assert len(msgs) == 1 and msgs[0].kind == "batch_reminder" and len(msgs[0].entries) == 7


def test_zero_threshold_lists_even_one_candidate():
    msgs = E.plan_recruiter_messages(_entries(7, 1), threshold=0)
    assert [m.kind for m in msgs] == ["batch_reminder"]
    assert [m.kind for m in E.plan_recruiter_messages(_entries(7, 1), threshold=None)] == \
        ["batch_reminder"]


def test_combine_off_keeps_each_kind():
    msgs = E.plan_recruiter_messages(_entries(7, 1) + _entries(7, 1, "final_validation"),
                                     threshold=0, combine=False)
    assert sorted(m.kind for m in msgs) == ["final_validation", "reminder"]


def test_reminder_params():
    rule = _seed_rules()[2]
    p = E.reminder_params(_item(), rule, 14, "Asha R", "https://x/clawback/health-checks?check=1",
                          _day_date(14))
    assert p["day_number"] == "14" and p["cooling_period_days"] == "90"
    assert p["clawback_completion_date"] == "2026-04-01"
    assert p["stage_label"] == "Days 8–30" and p["progress_percent"] == "14"
    pre = E.reminder_params(_item(cooling_period_days=None, joined_at=None,
                                  clawback_completion_date=None), rule, 3, None, "l", JOINED)
    assert pre["cooling_period_days"] == "-" and pre["recruiter_name"] == "-"


def test_recruiter_fallback():
    assert E.recruiter_for({"assigned_to": None, "recruiter_user_id": None, "created_by": 3}, True) == 3
    assert E.recruiter_for({"assigned_to": None, "recruiter_user_id": 3, "created_by": 3}, False) is None
