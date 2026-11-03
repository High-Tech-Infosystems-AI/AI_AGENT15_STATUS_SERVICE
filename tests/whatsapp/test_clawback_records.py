"""Records behind a dashboard number (contract §6.11)."""
from app.whatsapp_layer import clawback_records as R


def _case(cj, name, company="Acme", rec=7, rec_name="Asha", teams=((1, "Alpha"),), job=9,
          status="in_cooling", left=30, joined="2026-10-01", alerts=()):
    return {"candidate_job_id": cj, "candidate_id": f"C{cj}", "candidate_name": name,
            "company_name": company, "recruiter_user_id": rec, "recruiter_name": rec_name,
            "teams": [{"id": i, "name": n} for i, n in teams], "job_id": job, "job_title": "Sales",
            "case_status": status, "days_pending": left, "joined_at": joined,
            "offer_accepted_at": None, "alerts": list(alerts)}


ROWS = [
    _case(1, "Ravi", left=5, joined="2026-09-01"),
    _case(2, "Asha K", company="Beta", rec=8, rec_name="Meera", teams=(), status="dropped",
          left=None, joined="2026-10-05", alerts=("x",)),
    _case(3, "Zoya", job=10, teams=((1, "Alpha"), (2, "Beta team")), left=20, joined="2026-10-10"),
    _case(4, "Outside", left=1),
]


def _run(**kw):
    args = dict(filters={}, statuses=[], search=None, group_by="candidate", sort="days_left",
                page=1, page_size=10)
    args.update(kw)
    return R.records(ROWS, [1, 2, 3], **args)


def test_only_the_selected_ids_are_seen():
    out = _run()
    assert [c["candidate_job_id"] for c in out["items"]] == [1, 3, 2]   # no days left goes last
    assert out["selection"] == 3 and out["total"] == 3


def test_multi_select_filters_combine_within_and_across_dimensions():
    assert [c["candidate_name"] for c in _run(filters={"company": ["Acme", "Beta"]})["items"]] == \
        ["Ravi", "Zoya", "Asha K"]
    assert [c["candidate_name"] for c in _run(filters={"team": ["2"]})["items"]] == ["Zoya"]
    assert [c["candidate_name"] for c in _run(filters={"team": ["none"]})["items"]] == ["Asha K"]
    assert _run(filters={"company": ["Acme"], "recruiter": ["8"]})["total"] == 0
    assert [c["candidate_name"] for c in _run(filters={"job": ["10"]})["items"]] == ["Zoya"]
    assert _run(statuses=["dropped"])["total"] == 1


def test_search_covers_candidate_job_recruiter_and_team():
    assert _run(search=" meera ")["total"] == 1
    assert _run(search="beta team")["total"] == 1
    assert _run(search="c3")["items"][0]["candidate_name"] == "Zoya"


def test_sorts():
    names = lambda s: [c["candidate_name"] for c in _run(sort=s)["items"]]
    assert names("name") == ["Asha K", "Ravi", "Zoya"]
    assert names("joined_new") == ["Zoya", "Asha K", "Ravi"]
    assert names("joined_old") == ["Ravi", "Asha K", "Zoya"]


def test_pages_are_clamped():
    out = _run(page=9, page_size=2)
    assert (out["page"], out["pages"], len(out["items"])) == (2, 2, 1)


def test_group_by_team_counts_people_in_two_teams_in_both():
    out = _run(group_by="team", sort="count")
    assert [(g["label"], g["count"]) for g in out["items"]] == [
        ("Alpha", 2), ("Beta team", 1), ("No team", 1)]
    assert out["items"][0]["statuses"]["in_cooling"] == 2
    assert out["candidates"] == 3 and out["total"] == 3
    assert [g["label"] for g in _run(group_by="company", sort="name")["items"]] == ["Acme", "Beta"]
    assert _run(group_by="company", sort="name")["items"][1]["attention"] == 1


def test_facets_come_from_the_whole_selection_with_counts():
    out = _run(filters={"company": ["Beta"]})
    assert out["facets"]["company"] == [{"key": "Acme", "label": "Acme", "count": 2},
                                        {"key": "Beta", "label": "Beta", "count": 1}]
    assert {o["key"] for o in out["facets"]["team"]} == {"1", "2", "none"}
    assert out["status_counts"]["dropped"] == 1 and out["status_counts"]["in_cooling"] == 0


def test_endpoint_reads_the_viewers_dashboard_without_creating_checks(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app.dependencies.tenant_auth import AuthCtx
    from app.whatsapp_layer.api import health_checks_api as api
    from app.whatsapp_layer.schemas import DashboardRecords

    seen = {}

    def fake_dashboard(db, org, scope, refresh_checks=True):
        seen.update(org=org, scope=scope, refresh=refresh_checks)
        return {"cases": ROWS[:2], "offers": [ROWS[3]]}

    monkeypatch.setattr(api.dashboard_data, "build_dashboard", fake_dashboard)
    monkeypatch.setattr(api, "_subordinates", lambda org, uid: {8})
    ctx = AuthCtx(user_id=7, organization_id=1, role_id=2, role_name="Recruiter", role_type="user",
                  permissions={api.VIEW}, is_super_admin=False, is_admin=False, token="t")
    out = api.clawback_dashboard_records(DashboardRecords(ids=[1, 2, 3, 4], group_by="company"),
                                         None, ctx, None)
    assert seen == {"org": 1, "scope": {7, 8}, "refresh": False}
    assert out["selection"] == 3                       # case 3 is not on this viewer's dashboard
    assert [g["label"] for g in out["items"]] == ["Acme", "Beta"]
    with pytest.raises(HTTPException) as err:
        api.clawback_dashboard_records(DashboardRecords(group_by="company", sort="days_left"),
                                       None, ctx, None)
    assert err.value.status_code == 422
