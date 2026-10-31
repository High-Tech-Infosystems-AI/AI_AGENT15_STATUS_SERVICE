"""Clawback dashboard: case status, progress and alerts (contract §6.8)."""
from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.whatsapp_layer import clawback_dashboard as CD
from app.whatsapp_layer import people

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


def test_period_buckets():
    expected = {0.5: "<1 day", 1: "1 day", 1.5: "2-10 days", 2: "2-10 days", 10: "2-10 days",
                10.5: "11-20 days", 11: "11-20 days", 30: "21-30 days", 45: "41-50 days",
                80: "71-80 days", 81: "81-85 days", 85: "81-85 days", 86: "86-90 days",
                90: "86-90 days", 91: "90+ days", 365: "90+ days"}
    assert {d: CD.period_bucket(d) for d in expected} == expected
    assert _case(_row(cooling=45))["period_bucket"] == "41-50 days"


def test_drop_day_counts_from_joining():
    joined = datetime(2026, 10, 1)
    row = {**_row(ended="dropped"), "joined_at": joined, "ended_at": datetime(2026, 10, 15, 9)}
    c = _case(row)
    assert (c["drop_day"], c["drop_day_bucket"]) == (15, "11-20 days")
    assert _case({**row, "ended_at": joined})["drop_day"] == 1
    assert _case({**row, "ended_at": datetime(2026, 9, 1)})["drop_day"] == 1
    assert _case(_row())["drop_day"] is None


def test_offer_has_the_shape_of_a_case():
    item = {"candidate_job_id": 5, "candidate_id": "C5", "candidate_name": "Ravi", "job_id": 9,
            "job_title": "Sales", "company_name": "Acme", "status": "pre_joining", "assigned_to": 7,
            "recruiter_user_id": 7, "offer_accepted_at": "2026-10-20", "cooling_period_days": 45}
    offer = CD.build_offer(item, TODAY, {7: "Asha"})
    assert set(_case(_row())) - set(offer) == set()
    assert (offer["case_status"], offer["recruiter_name"]) == ("offer_accepted", "Asha")
    assert offer["offer_accepted_at"] == "2026-10-20" and offer["period_bucket"] == "41-50 days"
    assert offer["alerts"] == []


def test_finished_at_is_when_the_case_finished():
    done = _case(_row(ago=90, closed=datetime(2026, 10, 1, 5)))
    assert done["finished_at"] == "2026-10-01"
    dropped = {**_row(ended="dropped"), "ended_at": datetime(2026, 10, 20, 9)}
    assert _case(dropped)["finished_at"] == "2026-10-20"
    assert _case(_row())["finished_at"] is None


def test_teams_of_users_uses_active_rank_placements(wa_db):
    wa_db.execute(text("CREATE TABLE teams (id INT, name TEXT, deleted_at TEXT)"))
    wa_db.execute(text("CREATE TABLE user_org_assignment (organization_id INT, user_id INT, "
                       "unit_type TEXT, unit_id INT, deleted_at TEXT)"))
    wa_db.execute(text("INSERT INTO teams VALUES (1,'Alpha',NULL),(2,'Beta',NULL),(3,'Gone','x')"))
    wa_db.execute(text("INSERT INTO user_org_assignment VALUES "
                       "(9,7,'team',2,NULL),(9,7,'team',1,NULL),(9,7,'team',1,NULL),"   # Asha: two teams
                       "(9,7,'team',3,NULL),(9,7,'branch',2,NULL),"                     # deleted team, not a team
                       "(9,8,'team',1,'x'),(5,8,'team',1,NULL)"))                       # removed, other org
    assert people.teams_of_users(wa_db, 9, [7, 8]) == {
        7: [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}]}
    assert people.teams_of_users(wa_db, 9, []) == {}
