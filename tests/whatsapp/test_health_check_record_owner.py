"""Recording a check after the candidate moved to another recruiter."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.dependencies.tenant_auth import AuthCtx
from app.whatsapp_layer.api import health_checks_api as api
from app.whatsapp_layer.schemas import HealthCheckRecord


class FakeDB:
    def commit(self): pass
    def refresh(self, row): pass


def _ctx(uid):
    return AuthCtx(user_id=uid, organization_id=1, role_id=2, role_name="Recruiter",
                   role_type="user", permissions={api.RECORD}, is_super_admin=False,
                   is_admin=False, token="t")


@pytest.fixture
def check(monkeypatch):
    # Made for the old recruiter (137); the case now belongs to 114.
    chk = SimpleNamespace(id=4, organization_id=1, candidate_job_id=4831, recruiter_user_id=137,
                          status="missed", outcome=None, notes=None, recorded_by=None,
                          recorded_at=None)
    monkeypatch.setattr(api, "get_scoped", lambda db, model, cid, ctx, soft_delete: chk)
    monkeypatch.setattr(api, "_current_owner", lambda db, cj: 114)
    monkeypatch.setattr(api, "serialize_checks", lambda db, rows: [vars(r) for r in rows])
    monkeypatch.setattr(api, "_subordinates", lambda org, uid: {114} if uid == 106 else set())
    return chk


def _record(ctx):
    return api.record_health_check(4, HealthCheckRecord(outcome="engaged", notes="ok"), ctx, FakeDB())


def test_current_recruiter_can_record_a_check_made_for_the_previous_one(check):
    out = _record(_ctx(114))
    assert out["status"] == "done" and out["recorded_by"] == 114
    assert check.recruiter_user_id == 114           # handed over to the current recruiter


def test_current_recruiters_manager_can_record_it(check):
    assert _record(_ctx(106))["status"] == "done"


def test_previous_recruiter_can_still_record_it(check):
    assert _record(_ctx(137))["status"] == "done"


def test_someone_else_cannot(check):
    with pytest.raises(HTTPException) as err:
        _record(_ctx(999))
    assert err.value.status_code == 403 and check.status == "missed"
