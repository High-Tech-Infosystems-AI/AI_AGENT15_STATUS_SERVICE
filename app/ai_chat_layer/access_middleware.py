"""Access middleware — the single security boundary for AI tool calls.

Every data tool MUST go through `apply_scope()` before issuing its query.
Middleware loads the caller's identity, decides whether the role gets full
visibility, and (for non-admins) returns the filter clauses tools should
splice into their SQL.

Roles:
    super_admin / admin → no scope filter (see all jobs/candidates/companies)
    everyone else       → only entities tied to job assignments / pipelines
                          they personally have

The recruiter scope is computed once per request and cached in Redis for
60 seconds keyed on user_id.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.chat_layer.chat_acl import is_admin
from app.notification_layer import redis_manager

logger = logging.getLogger("app_logger")

_SCOPE_TTL = 60


class AccessDeniedError(Exception):
    """Raised when middleware proves the caller cannot see the entity."""

    def __init__(self, entity_type: str, entity_id):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"access denied for {entity_type} {entity_id}")


@dataclass
class CallerScope:
    """Outcome of `apply_scope()` — the rules tools must respect."""
    user_id: int
    role_name: Optional[str]
    is_admin: bool
    job_ids: Set[int] = field(default_factory=set)
    candidate_ids: Set[str] = field(default_factory=set)
    company_ids: Set[int] = field(default_factory=set)

    @property
    def unscoped(self) -> bool:
        """Admin/SuperAdmin see everything."""
        return self.is_admin

    def filter_job_ids(self, ids: List[int]) -> List[int]:
        if self.unscoped:
            return list(ids)
        return [i for i in ids if i in self.job_ids]

    def filter_candidate_ids(self, ids: List[str]) -> List[str]:
        if self.unscoped:
            return list(ids)
        return [i for i in ids if i in self.candidate_ids]

    def filter_company_ids(self, ids: List[int]) -> List[int]:
        if self.unscoped:
            return list(ids)
        return [i for i in ids if i in self.company_ids]

    def has_job(self, job_id: int) -> bool:
        return self.unscoped or job_id in self.job_ids

    def has_candidate(self, cand_id: str) -> bool:
        return self.unscoped or cand_id in self.candidate_ids

    def has_company(self, company_id: int) -> bool:
        return self.unscoped or company_id in self.company_ids


def _cache_key(user_id: int) -> str:
    return f"ai:scope:{user_id}"


def _load_recruiter_scope(db: Session, user_id: int) -> dict:
    """Pull job/candidate/company ids the recruiter can see. Single round-trip
    per category, all uncached SQL — caller wraps with Redis cache."""
    job_ids = [
        int(r[0]) for r in db.execute(
            text("SELECT job_id FROM user_jobs_assigned WHERE user_id = :uid"),
            {"uid": user_id},
        ).all()
    ]
    cand_ids: List[str] = []
    company_ids: List[int] = []
    if job_ids:
        cand_rows = db.execute(
            text(
                "SELECT DISTINCT candidate_id FROM candidate_jobs WHERE job_id IN :ids",
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": job_ids},
        ).all()
        cand_ids = [str(r[0]) for r in cand_rows if r[0] is not None]

        co_rows = db.execute(
            text(
                "SELECT DISTINCT company_id FROM job_openings "
                "WHERE id IN :ids AND company_id IS NOT NULL",
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": job_ids},
        ).all()
        company_ids = [int(r[0]) for r in co_rows]
    return {
        "job_ids": job_ids,
        "candidate_ids": cand_ids,
        "company_ids": company_ids,
    }


def apply_scope(db: Session, user: dict) -> CallerScope:
    """Resolve the caller's CallerScope. Reads from Redis cache when warm."""
    user_id = int(user.get("user_id"))
    role_name = user.get("role_name")
    admin = is_admin(role_name)
    if admin:
        return CallerScope(user_id=user_id, role_name=role_name, is_admin=True)

    cached = None
    try:
        raw = redis_manager.get_notification_redis().get(_cache_key(user_id))
        if raw:
            cached = json.loads(raw)
    except Exception:
        cached = None

    if cached is None:
        cached = _load_recruiter_scope(db, user_id)
        try:
            redis_manager.get_notification_redis().setex(
                _cache_key(user_id), _SCOPE_TTL, json.dumps(cached, default=str),
            )
        except Exception:
            pass

    return CallerScope(
        user_id=user_id,
        role_name=role_name,
        is_admin=False,
        job_ids=set(int(x) for x in cached.get("job_ids", []) if x is not None),
        candidate_ids=set(str(x) for x in cached.get("candidate_ids", []) if x is not None),
        company_ids=set(int(x) for x in cached.get("company_ids", []) if x is not None),
    )


def invalidate_scope(user_id: int) -> None:
    try:
        redis_manager.get_notification_redis().delete(_cache_key(user_id))
    except Exception:
        pass


def assert_can_see_ref(scope: CallerScope, ref: dict) -> None:
    """Validate a single (type, id) ref against the caller's scope.

    Pipelines, users, teams, and reports are universally readable; only
    job/candidate/company need a check.
    """
    if scope.unscoped:
        return
    rtype = ref.get("type")
    rid = ref.get("id")
    if rtype == "job":
        try:
            if not scope.has_job(int(rid)):
                raise AccessDeniedError(rtype, rid)
        except (TypeError, ValueError):
            raise AccessDeniedError(rtype, rid)
    elif rtype == "candidate":
        if not scope.has_candidate(str(rid)):
            raise AccessDeniedError(rtype, rid)
    elif rtype == "company":
        try:
            if not scope.has_company(int(rid)):
                raise AccessDeniedError(rtype, rid)
        except (TypeError, ValueError):
            raise AccessDeniedError(rtype, rid)
