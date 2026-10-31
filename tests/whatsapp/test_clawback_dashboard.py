"""Clawback dashboard: case status, progress and alerts (contract §6.8)."""
from datetime import date, datetime, timedelta

from app.whatsapp_layer import clawback_dashboard as CD

TODAY = date(2026, 10, 30)


def _row(ago=10, cooling=90, ended=None, closed=None, assigned=7):
    return {"id": 1, "job_id": 9, "candidate_id": "C1", "candidate_name": "Ravi", "assigned_to": assigned,
            "created_by": 3, "title": "Sales", "company_id": 2, "company_name": "Acme",
            "cooling_period": cooling, "joined_at": datetime.combine(TODAY - timedelta(days=ago), datetime.min.time()),
            "cooling_period_closed": closed, "ended_type": ended}


def _case(row, check=None):
    case = CD.build_case(row, check, TODAY, {7: "Asha", 3: "Creator"})
    case["alerts"] = CD.alerts_for(case, TODAY)
    return case


def test_in_cooling_case_has_progress_and_no_alert():
    c = _case(_row())
    assert (c["case_status"], c["days_pending"], c["progress_percent"], c["cooling_period_days"]) == ("in_cooling", 80, 11, 90)
    assert c["recruiter_name"] == "Asha" and c["day_number"] == 11 and c["alerts"] == []


def test_recruiter_falls_back_to_creator():
    assert _case(_row(assigned=None))["recruiter_name"] == "Creator"


def test_cooling_ended_and_ending_soon_alert():
    assert _case(_row(ago=90))["alerts"] == ["final_pending"]
    assert _case(_row(ago=85))["alerts"] == ["ending_soon"]


def test_finished_cases_do_not_alert():
    assert _case(_row(ago=90, closed=datetime(2026, 10, 1)))["case_status"] == "completed"
    c = _case(_row(ended="dropped"), {"id": 1, "status": "pending", "due_date": TODAY - timedelta(days=2)})
    assert c["case_status"] == "dropped" and c["alerts"] == []
    assert _case(_row(ago=90, closed=datetime(2026, 10, 1)))["progress_percent"] == 100


def test_check_alerts():
    overdue = {"id": 5, "status": "missed", "due_date": TODAY - timedelta(days=2)}
    c = _case(_row(), overdue)
    assert c["alerts"] == ["overdue"] and c["overdue"] and c["check_id"] == 5
    assert _case(_row(), {"id": 6, "status": "pending", "due_date": TODAY})["alerts"] == ["due_today"]
    assert _case(_row(), {"id": 7, "status": "done", "outcome": "at_risk", "due_date": TODAY})["alerts"] == ["at_risk"]
    assert _case(_row(), {"id": 8, "status": "done", "outcome": "engaged", "due_date": TODAY})["alerts"] == []


def test_day_number_stops_at_the_end_of_the_period():
    assert _case(_row(ago=200, cooling=30))["day_number"] == 30


def test_part_day_period_ends_instead_of_staying_in_cooling():
    c = _case(_row(ago=200, cooling=0.5))
    assert (c["case_status"], c["days_pending"], c["progress_percent"]) == ("cooling_ended", 0, 100)
    assert c["completion_date"] == (TODAY - timedelta(days=199)).isoformat()
    assert c["day_number"] == 1 and c["alerts"] == ["final_pending"]
    assert _case(_row(ago=0, cooling=0.5))["case_status"] == "in_cooling"


def test_finished_at_is_when_the_case_finished():
    done = _case(_row(ago=90, closed=datetime(2026, 10, 1, 5)))
    assert done["finished_at"] == "2026-10-01"
    dropped = {**_row(ended="dropped"), "ended_at": datetime(2026, 10, 20, 9)}
    assert _case(dropped)["finished_at"] == "2026-10-20"
    assert _case(_row())["finished_at"] is None
