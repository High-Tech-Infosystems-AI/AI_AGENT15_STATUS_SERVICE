"""Curated read-only data tools for the AI agent.

Each tool:
  - takes typed args (Pydantic),
  - calls the access middleware to get the caller's scope,
  - issues a parameterized query through the MCP client,
  - returns serializable JSON,
  - logs an entry in `ctx.trace` with timing.

Tools NEVER let the model write SQL — only fixed query templates with
parameters live here. Adding a new question type = add a new tool.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

from app.ai_chat_layer.mcp_client import SchemaUnavailableError
from app.ai_chat_layer.mcp_server.elicitation import (
    ElicitationField, ElicitationOption, ElicitationRequired,
    ElicitationSpec, make_elicitation,
)
from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scope_job_filter(ctx: ToolContext, alias: str = "j") -> tuple[str, dict]:
    """Return ("AND alias.id IN :scope_jobs", {scope_jobs: [...]}) or empty."""
    if ctx.scope.unscoped:
        return "", {}
    if not ctx.scope.job_ids:
        # Recruiter has zero jobs: tools should return empty without
        # raising — middleware just removes everything.
        return f" AND {alias}.id IN (-1)", {}
    return f" AND {alias}.id IN :scope_jobs", {"scope_jobs": list(ctx.scope.job_ids)}


def _scope_candidate_filter(ctx: ToolContext, alias: str = "c") -> tuple[str, dict]:
    if ctx.scope.unscoped:
        return "", {}
    if not ctx.scope.candidate_ids:
        return f" AND {alias}.candidate_id IN ('__NONE__')", {}
    return f" AND {alias}.candidate_id IN :scope_cands", {"scope_cands": list(ctx.scope.candidate_ids)}


def _handle_elicitation(ctx: ToolContext, payload: Dict[str, Any]) -> Dict[str, Any]:
    """If a tool's return contains `elicitation_required`, attach the form
    spec to the reply as an `ai_elicitation` ref and substitute a small
    "pending" payload for the model so it acknowledges and waits."""
    spec = payload.get("elicitation_required")
    if not isinstance(spec, dict):
        return payload
    ctx.add_output_ref({
        "type": "ai_elicitation",
        "id": spec.get("id"),
        "params": spec,
    })
    return {
        "elicitation_pending": True,
        "elicitation_id": spec.get("id"),
        "title": spec.get("title"),
        "note": payload.get("note") or (
            "Awaiting user input. Reply with a brief one-line acknowledgment "
            "and stop — the user will submit the form and your next turn "
            "will receive their answer."
        ),
    }


def _timed(ctx: ToolContext, name: str, args: Dict[str, Any], fn):
    start = time.monotonic()
    try:
        out = fn()
        # Server-side elicitation: tools may either return the dict shape
        # or raise ElicitationRequired — both convert to the same ref.
        if isinstance(out, dict):
            out = _handle_elicitation(ctx, out)
        ctx.add_trace(name, args, int((time.monotonic() - start) * 1000), True)
        return out
    except ElicitationRequired as exc:
        payload = make_elicitation(exc.spec, note=exc.note)
        out = _handle_elicitation(ctx, payload)
        ctx.add_trace(name, args, int((time.monotonic() - start) * 1000), True)
        return out
    except SchemaUnavailableError as exc:
        # The query referenced a column / table that doesn't exist in the
        # deployed schema. Surface a structured "data unavailable" payload
        # so the model summarizes what it CAN answer instead of refusing.
        ctx.add_trace(
            name, args, int((time.monotonic() - start) * 1000), False,
            f"schema_unavailable: {exc.missing or 'unknown'}",
        )
        logger.warning("tool %s schema unavailable: %s", name, exc.missing)
        return {
            "data_unavailable": True,
            "reason": "schema_mismatch",
            "missing": exc.missing,
            "items": [],
            "note": (
                f"The field/table '{exc.missing}' isn't tracked in this "
                "workspace. Continue the answer with whatever other data "
                "you have — do not refuse the request."
                if exc.missing else
                "This deployment's schema doesn't include the data the tool "
                "needed. Continue with a partial answer; do not refuse."
            ),
        }
    except Exception as exc:
        ctx.add_trace(name, args, int((time.monotonic() - start) * 1000), False, str(exc))
        logger.exception("tool %s failed", name)
        return {"error": str(exc), "items": []}


# ---------------------------------------------------------------------------
# Args schemas
# ---------------------------------------------------------------------------

class DateRange(BaseModel):
    date_from: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD")
    date_to: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD")


class ListJobsArgs(DateRange):
    company_id: Optional[int] = None
    status: Optional[str] = Field(default=None, description="ACTIVE | CLOSED | ON_HOLD …")
    location: Optional[str] = Field(
        default=None, description="Restrict to one job location."
    )
    work_mode: Optional[str] = Field(
        default=None, description="ONSITE | REMOTE | HYBRID."
    )
    deadline_before: Optional[str] = Field(
        default=None, description="ISO date YYYY-MM-DD; jobs with deadline on/before."
    )
    deadline_after: Optional[str] = Field(
        default=None, description="ISO date YYYY-MM-DD; jobs with deadline on/after."
    )
    has_applicants: Optional[bool] = Field(
        default=None,
        description=(
            "True → only jobs with at least one application. "
            "False → only jobs with ZERO applications. "
            "Omit to include both."
        ),
    )
    has_recruiter: Optional[bool] = Field(
        default=None,
        description=(
            "True → only jobs with at least one recruiter assigned. "
            "False → only jobs with NO recruiter assigned. "
            "Omit to include both."
        ),
    )
    limit: int = Field(default=20, ge=1, le=100)


class ListCandidatesArgs(DateRange):
    job_id: Optional[int] = None
    stage: Optional[str] = None
    limit: int = Field(default=20, ge=1, le=100)


class JobOnlyArgs(BaseModel):
    job_id: int


class CandidateOnlyArgs(BaseModel):
    candidate_id: str


class CompanyOnlyArgs(BaseModel):
    company_id: int


class RecruiterMetricsArgs(DateRange):
    user_id: Optional[int] = None


class CountByStageArgs(DateRange):
    job_id: Optional[int] = None
    company_id: Optional[int] = None


class TopRecruitersArgs(DateRange):
    limit: int = Field(default=10, ge=1, le=50)


class CompanyJobsArgs(DateRange):
    limit: int = Field(default=20, ge=1, le=100)


class SearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=120)
    limit: int = Field(default=10, ge=1, le=50)
    disambiguate_kind: Optional[
        Literal["job", "candidate", "company", "user", "team", "pipeline"]
    ] = Field(
        default=None,
        description=(
            "When set, the tool focuses on this entity kind. If the search "
            "returns a SINGLE match, you get a `resolved` payload with the "
            "id ready to use. If MULTIPLE matches, the tool surfaces an "
            "elicitation form so the user picks one, and the next turn "
            "delivers the chosen id back. If ZERO matches, the tool "
            "returns `not_found` and you should say so plainly. Use this "
            "whenever the user names a person / candidate / company / job "
            "/ team without tagging them — it removes the ambiguity in "
            "one round-trip."
        ),
    )


class DashboardArgs(DateRange):
    chart_id: str
    company_id: Optional[int] = None
    job_id: Optional[int] = None
    user_id: Optional[int] = None


# ─── New args schemas for the v2 tool surface ───────────────────────────

class FunnelArgs(DateRange):
    """Pipeline funnel scoped by job / company / user / team / global."""
    scope: Literal["job", "company", "user", "team", "global"] = "global"
    scope_id: Optional[int] = None
    limit_per_tag: int = Field(default=5, ge=1, le=25)


class TeamArgs(BaseModel):
    team_id: int


class UserArgs(BaseModel):
    user_id: int


class UserCompareArgs(DateRange):
    user_ids: List[int] = Field(..., min_length=2, max_length=8)


class UserSourcingArgs(DateRange):
    user_id: int
    limit: int = Field(default=20, ge=1, le=100)


class LatestResumesArgs(BaseModel):
    """Args for `latest_resumes`."""
    limit: int = Field(default=10, ge=1, le=100)
    candidate_id: Optional[str] = Field(
        default=None,
        description="Restrict to one candidate's resumes (all versions).",
    )
    job_id: Optional[int] = Field(
        default=None,
        description=(
            "Restrict to candidates currently applied to this job. "
            "Returns latest resume per candidate."
        ),
    )


class CandidateResumeDetailArgs(BaseModel):
    candidate_id: str
    resume_version: Optional[int] = Field(
        default=None,
        description="Specific version. Omit for the latest version.",
    )


class CandidateActivityArgs(BaseModel):
    candidate_id: str
    type: Optional[
        Literal["general", "pipeline", "accepted", "status", "rejected"]
    ] = Field(default=None, description="Filter to one event type.")
    limit: int = Field(default=20, ge=1, le=100)


class RecentActivityFeedArgs(BaseModel):
    scope: Literal["candidate", "job", "company", "recruiter", "team", "global"] = (
        Field(default="global", description="Restrict the feed to events under one entity.")
    )
    scope_id: Optional[Any] = Field(
        default=None,
        description=(
            "Id whose meaning depends on scope: int for job / company / "
            "recruiter / team, candidate_id (string) for candidate. "
            "Omit when scope='global'."
        ),
    )
    type: Optional[
        Literal["general", "pipeline", "accepted", "status", "rejected"]
    ] = Field(default=None, description="Filter to one event type.")
    acted_by_user_id: Optional[int] = Field(
        default=None,
        description="Only events whose actor (candidate_activity.user_id) is this user.",
    )
    since_days: Optional[int] = Field(
        default=None, ge=1, le=180,
        description="Only events newer than N days ago.",
    )
    limit: int = Field(default=25, ge=1, le=100)


class ListPipelinesArgs(BaseModel):
    active_only: bool = Field(
        default=True,
        description="Hide soft-deleted pipelines (deleted_at IS NOT NULL).",
    )
    limit: int = Field(default=50, ge=1, le=200)


class PipelineDetailArgs(BaseModel):
    pipeline_id: int = Field(..., description="Integer PK of pipelines table.")


class CandidateStageHistoryArgs(BaseModel):
    candidate_id: str
    job_id: Optional[int] = Field(
        default=None,
        description="Restrict history to one job. Omit for every job.",
    )
    limit: int = Field(default=50, ge=1, le=200)


class ListTeamsArgs(DateRange):
    department: Optional[str] = Field(
        default=None,
        description="Filter by department (case-insensitive exact match).",
    )
    status: Optional[str] = Field(
        default=None,
        description="'active' / 'inactive' (case-insensitive).",
    )
    search: Optional[str] = Field(
        default=None,
        description="Substring on team name or description.",
    )
    has_jobs: Optional[bool] = Field(
        default=None,
        description=(
            "True → only teams with at least one job assignment. "
            "False → only teams with NO job assignments."
        ),
    )
    has_members: Optional[bool] = Field(
        default=None,
        description=(
            "True → only teams with at least one member. "
            "False → only teams with NO members."
        ),
    )
    include_deleted: bool = Field(
        default=False,
        description="If True, include soft-deleted teams.",
    )
    limit: int = Field(default=50, ge=1, le=200)


class TeamCompareArgs(DateRange):
    team_ids: List[int] = Field(..., min_length=2, max_length=8)


class ListUsersArgs(DateRange):
    role: Optional[str] = Field(
        default=None,
        description="Filter by role name (super_admin / admin / user). Case-insensitive.",
    )
    enabled: Optional[bool] = Field(
        default=True,
        description=(
            "True (default) → only enabled users. False → only disabled. "
            "Pass null/None to include both."
        ),
    )
    include_deleted: bool = Field(
        default=False,
        description="If True, include soft-deleted users.",
    )
    team_id: Optional[int] = Field(
        default=None,
        description="Restrict to members of this team.",
    )
    search: Optional[str] = Field(
        default=None,
        description="Substring on name / username / email.",
    )
    limit: int = Field(default=50, ge=1, le=200)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

def _list_jobs(ctx: ToolContext, args: ListJobsArgs) -> Dict[str, Any]:
    where: List[str] = ["1=1"]
    params: Dict[str, Any] = {}

    if args.status:
        where.append("UPPER(j.status) = :status")
        params["status"] = args.status.upper()
    if args.company_id is not None:
        where.append("j.company_id = :company_id")
        params["company_id"] = args.company_id
    if args.location:
        where.append("j.location = :location")
        params["location"] = args.location
    if args.work_mode:
        where.append("UPPER(j.work_mode) = UPPER(:work_mode)")
        params["work_mode"] = args.work_mode
    if args.deadline_before:
        where.append("j.deadline <= :deadline_before")
        params["deadline_before"] = args.deadline_before
    if args.deadline_after:
        where.append("j.deadline >= :deadline_after")
        params["deadline_after"] = args.deadline_after
    if args.date_from:
        where.append("j.created_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("j.created_at <= :date_to")
        params["date_to"] = args.date_to
    if args.has_applicants is True:
        where.append(
            "EXISTS (SELECT 1 FROM candidate_jobs cj2 WHERE cj2.job_id = j.id)"
        )
    elif args.has_applicants is False:
        where.append(
            "NOT EXISTS (SELECT 1 FROM candidate_jobs cj2 WHERE cj2.job_id = j.id)"
        )
    if args.has_recruiter is True:
        where.append(
            "EXISTS (SELECT 1 FROM user_jobs_assigned uja2 WHERE uja2.job_id = j.id)"
        )
    elif args.has_recruiter is False:
        where.append(
            "NOT EXISTS (SELECT 1 FROM user_jobs_assigned uja2 WHERE uja2.job_id = j.id)"
        )

    scope_clause, scope_params = _scope_job_filter(ctx)
    if scope_clause:
        where.append(scope_clause.lstrip(" AND "))
    params.update(scope_params)
    params["_limit"] = args.limit

    sql = f"""
        SELECT j.id, j.job_id, j.title, j.status, j.openings,
               j.location, j.work_mode, j.deadline, j.created_at,
               co.company_name AS company_name, co.id AS company_id
          FROM job_openings j
     LEFT JOIN companies co ON co.id = j.company_id
         WHERE {" AND ".join(where)}
         ORDER BY j.created_at DESC
         LIMIT :_limit
    """.strip()
    expanding = ["scope_jobs"] if "scope_jobs" in scope_params else None
    rows = ctx.mcp.query(sql, params, expanding_keys=expanding)
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "external_id": r.get("job_id"),
            "title": r.get("title"),
            "status": (r.get("status") or "").upper() or None,
            "openings": r.get("openings"),
            "location": r.get("location"),
            "work_mode": r.get("work_mode"),
            "deadline": str(r.get("deadline")) if r.get("deadline") else None,
            "company_id": r.get("company_id"),
            "company_name": r.get("company_name"),
            "created_at": str(r.get("created_at")) if r.get("created_at") else None,
        })
        ctx.add_output_ref({"type": "job", "id": r["id"]})
    return {"items": items, "count": len(items)}


def _job_detail(ctx: ToolContext, args: JobOnlyArgs) -> Dict[str, Any]:
    if not ctx.scope.has_job(args.job_id):
        return {"access_denied": True, "type": "job", "id": args.job_id}
    rows = ctx.mcp.query(
        """
        SELECT j.id, j.job_id, j.title, j.status, j.stage, j.deadline,
               j.openings, j.location, j.work_mode, j.created_at,
               j.pipeline_id,
               co.id AS company_id, co.company_name,
               pl.name AS pipeline_name,
               (SELECT COUNT(*) FROM candidate_jobs cj WHERE cj.job_id = j.id) AS applicant_count,
               (SELECT COUNT(*) FROM user_jobs_assigned uja WHERE uja.job_id = j.id) AS recruiter_count
          FROM job_openings j
     LEFT JOIN companies co ON co.id = j.company_id
     LEFT JOIN pipelines pl ON pl.id = j.pipeline_id
         WHERE j.id = :jid
        """,
        {"jid": args.job_id},
    )
    if not rows:
        return {"not_found": True}
    r = rows[0]
    ctx.add_output_ref({"type": "job", "id": r["id"]})
    if r.get("company_id"):
        ctx.add_output_ref({"type": "company", "id": r["company_id"]})
    return {
        "id": r["id"], "external_id": r.get("job_id"),
        "title": r.get("title"), "status": (r.get("status") or "").upper() or None,
        "stage": r.get("stage"), "deadline": str(r.get("deadline")) if r.get("deadline") else None,
        "openings": r.get("openings"), "location": r.get("location"),
        "work_mode": r.get("work_mode"),
        "company": {"id": r.get("company_id"), "name": r.get("company_name")},
        "pipeline": (
            {"id": r.get("pipeline_id"), "name": r.get("pipeline_name")}
            if r.get("pipeline_id") else None
        ),
        "applicant_count": r.get("applicant_count"),
        "recruiter_count": r.get("recruiter_count"),
        "created_at": str(r.get("created_at")) if r.get("created_at") else None,
    }


def _candidate_detail(ctx: ToolContext, args: CandidateOnlyArgs) -> Dict[str, Any]:
    """Full candidate profile — every column from `candidates`, plus the
    latest resume header / match score / top skills, recent activity, and
    every job pipeline link. Use this whenever the model needs to do
    analysis on a candidate; it ships all available context in one call
    so the model never has to chain follow-up tools just to see profile
    data.

    Pipeline links (one row per candidate_jobs) include the current stage
    and outcome tag from candidate_pipeline_status / pipeline_stage_status.
    """
    cand_id = str(args.candidate_id)
    if not ctx.scope.has_candidate(cand_id):
        return {"access_denied": True, "type": "candidate", "id": cand_id}

    rows = ctx.mcp.query(
        """
        SELECT c.candidate_id            AS id,
               c.candidate_name          AS name,
               c.candidate_email         AS email,
               c.candidate_phone_number  AS phone,
               c.candidate_linkedIn      AS linkedin,
               c.portfolio,
               c.employment_status, c.employment_type,
               c.current_work_mode, c.work_mode_prefer,
               c.experience,
               c.current_company, c.current_location,
               c.home_town, c.preferred_location,
               c.current_salary, c.current_salary_curr,
               c.expected_salary, c.expected_salary_curr,
               c.on_notice, c.available_from,
               c.year_of_graduation, c.dob, c.age, c.gender,
               c.skills, c.industries_worked_on, c.employment_gap,
               c.profile_source, c.creation_source,
               c.job_profile, c.assigned_to,
               c.created_by, c.created_at, c.updated_at,
               (SELECT cs.candidate_status
                  FROM candidate_status cs
                 WHERE cs.candidate_id = c.candidate_id
                 ORDER BY cs.updated_at DESC, cs.id DESC
                 LIMIT 1) AS latest_status,
               (SELECT cs.remarks
                  FROM candidate_status cs
                 WHERE cs.candidate_id = c.candidate_id
                 ORDER BY cs.updated_at DESC, cs.id DESC
                 LIMIT 1) AS latest_status_remarks,
               (SELECT COUNT(*) FROM candidate_jobs cj
                 WHERE cj.candidate_id = c.candidate_id) AS job_count,
               (SELECT COUNT(*) FROM resume_personal_details rp
                 WHERE rp.candidate_id = c.candidate_id) AS resume_versions_count,
               (SELECT MAX(rp.resume_version)
                  FROM resume_personal_details rp
                 WHERE rp.candidate_id = c.candidate_id) AS latest_resume_version,
               (SELECT rm.overall_match_score
                  FROM resume_matching rm
                 WHERE rm.candidate_id = c.candidate_id
                 ORDER BY rm.resume_version DESC
                 LIMIT 1) AS latest_match_score,
               u.name AS assigned_to_name
          FROM candidates c
     LEFT JOIN users u ON u.id = c.assigned_to
         WHERE c.candidate_id = :cid
         LIMIT 1
        """,
        {"cid": cand_id},
    )
    if not rows:
        return {"not_found": True, "type": "candidate", "id": cand_id}
    p = rows[0]

    # Latest resume header + top skills + match score breakdown.
    latest_version = p.get("latest_resume_version")
    resume_summary: Optional[Dict[str, Any]] = None
    if latest_version is not None:
        rp_rows = ctx.mcp.query(
            """
            SELECT rp.resume_id, rp.resume_version, rp.summary,
                   rp.languages, rp.webpage,
                   rm.overall_match_score, rm.skills_match_percentage,
                   rm.qualification_match_percentage,
                   rm.experience_match_percentage,
                   rm.designation_match_percentage,
                   rm.matched_skills, rm.missing_skills, rm.extra_skills,
                   rm.recommendation,
                   rp.created_at
              FROM resume_personal_details rp
         LEFT JOIN resume_matching rm
                ON rm.candidate_id = rp.candidate_id
               AND rm.resume_version = rp.resume_version
             WHERE rp.candidate_id = :cid AND rp.resume_version = :ver
             LIMIT 1
            """,
            {"cid": cand_id, "ver": latest_version},
        )
        if rp_rows:
            rr = rp_rows[0]
            skill_rows = ctx.mcp.query(
                """
                SELECT skill_category, skill_name
                  FROM resume_skills
                 WHERE resume_id = :rid
                 ORDER BY skill_category, skill_name
                 LIMIT 50
                """,
                {"rid": rr["resume_id"]},
            )
            skills = {"technical": [], "soft": [], "other": []}
            for s in skill_rows:
                cat = (s.get("skill_category") or "other")
                if cat not in skills:
                    skills[cat] = []
                if s.get("skill_name"):
                    skills[cat].append(s["skill_name"])
            resume_summary = {
                "resume_id": rr.get("resume_id"),
                "version": rr.get("resume_version"),
                "summary": rr.get("summary"),
                "languages": rr.get("languages"),
                "webpage": rr.get("webpage"),
                "created_at": (
                    str(rr.get("created_at")) if rr.get("created_at") else None
                ),
                "skills": skills,
                "match": {
                    "overall": rr.get("overall_match_score"),
                    "skills_pct": rr.get("skills_match_percentage"),
                    "qualification_pct": rr.get("qualification_match_percentage"),
                    "experience_pct": rr.get("experience_match_percentage"),
                    "designation_pct": rr.get("designation_match_percentage"),
                    "matched_skills": rr.get("matched_skills"),
                    "missing_skills": rr.get("missing_skills"),
                    "extra_skills": rr.get("extra_skills"),
                    "recommendation": rr.get("recommendation"),
                } if rr.get("overall_match_score") is not None else None,
            }

    # Last 5 activity rows for quick context.
    activity_rows = ctx.mcp.query(
        """
        SELECT ca.id, ca.type, ca.remark, ca.key_id, ca.created_at,
               u.name AS actor_name
          FROM candidate_activity ca
     LEFT JOIN users u ON u.id = ca.user_id
         WHERE ca.candidate_id = :cid
         ORDER BY ca.created_at DESC, ca.id DESC
         LIMIT 5
        """,
        {"cid": cand_id},
    )
    recent_activity = [
        {
            "id": r.get("id"),
            "type": r.get("type"),
            "remark": r.get("remark"),
            "key_id": r.get("key_id"),
            "actor": r.get("actor_name"),
            "created_at": (
                str(r.get("created_at")) if r.get("created_at") else None
            ),
        }
        for r in activity_rows
    ]

    # Candidate's pipeline links — one row per candidate_jobs with their
    # current stage and outcome tag. Useful for "where is X in the
    # pipeline" / "what offers do they have" questions.
    pipeline_rows = ctx.mcp.query(
        """
        SELECT cj.id        AS candidate_job_id,
               cj.job_id    AS job_id,
               j.job_id     AS job_external_id,
               j.title      AS job_title,
               j.status     AS job_status,
               co.company_name AS company_name,
               cj.created_at AS applied_at,
               ps.id        AS stage_id,
               ps.name      AS stage,
               ps.`order`   AS stage_order,
               cps.status   AS status_option,
               pss.tag      AS outcome_tag
          FROM candidate_jobs cj
          LEFT JOIN job_openings j ON j.id = cj.job_id
          LEFT JOIN companies co ON co.id = j.company_id
          LEFT JOIN candidate_pipeline_status cps
                 ON cps.candidate_job_id = cj.id AND cps.latest = 1
          LEFT JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
          LEFT JOIN pipeline_stage_status pss
                 ON pss.pipeline_stage_id = cps.pipeline_stage_id
                AND UPPER(pss.option) = UPPER(cps.status)
         WHERE cj.candidate_id = :cid
         ORDER BY cj.created_at DESC
        """,
        {"cid": cand_id},
    )
    pipeline = []
    for r in pipeline_rows:
        pipeline.append({
            "candidate_job_id": r.get("candidate_job_id"),
            "job_id": r.get("job_id"),
            "job_external_id": r.get("job_external_id"),
            "job_title": r.get("job_title"),
            "job_status": (r.get("job_status") or "").upper() or None,
            "company_name": r.get("company_name"),
            "applied_at": (
                str(r.get("applied_at")) if r.get("applied_at") else None
            ),
            "stage": r.get("stage"),
            "status": r.get("status_option"),
            "outcome": r.get("outcome_tag"),
        })

    ctx.add_output_ref({"type": "candidate", "id": cand_id})

    # Surface a chip card for the most recent job too, so the user can
    # one-click into the related kanban from the candidate's reply.
    if pipeline and pipeline[0].get("job_id"):
        ctx.add_output_ref({"type": "job", "id": pipeline[0]["job_id"]})

    def _s(val):
        return str(val) if val is not None else None

    return {
        # Identity
        "id": p["id"],
        "name": p.get("name"),
        "email": p.get("email"),
        "phone": p.get("phone"),
        "linkedin": p.get("linkedin"),
        "portfolio": p.get("portfolio"),
        # Employment / preferences
        "employment_status": p.get("employment_status"),
        "employment_type": p.get("employment_type"),
        "current_work_mode": p.get("current_work_mode"),
        "preferred_work_mode": p.get("work_mode_prefer"),
        "experience_years": p.get("experience"),
        "current_company": p.get("current_company"),
        "current_location": p.get("current_location"),
        "home_town": p.get("home_town"),
        "preferred_location": p.get("preferred_location"),
        # Compensation
        "current_salary": p.get("current_salary"),
        "current_salary_curr": p.get("current_salary_curr"),
        "expected_salary": p.get("expected_salary"),
        "expected_salary_curr": p.get("expected_salary_curr"),
        # Availability
        "on_notice": bool(p.get("on_notice")) if p.get("on_notice") is not None else None,
        "available_from": _s(p.get("available_from")),
        "employment_gap": p.get("employment_gap"),
        # Demographics
        "year_of_graduation": p.get("year_of_graduation"),
        "dob": _s(p.get("dob")),
        "age": p.get("age"),
        "gender": p.get("gender"),
        # Skills / industries
        "skills": p.get("skills"),
        "industries_worked_on": p.get("industries_worked_on"),
        # Sourcing / ownership
        "profile_source": p.get("profile_source"),
        "creation_source": p.get("creation_source"),
        "job_profile": p.get("job_profile"),
        "assigned_to": (
            {"id": p.get("assigned_to"), "name": p.get("assigned_to_name")}
            if p.get("assigned_to") else None
        ),
        "created_by": p.get("created_by"),
        "created_at": _s(p.get("created_at")),
        "updated_at": _s(p.get("updated_at")),
        # Status / activity / pipeline
        "latest_status": p.get("latest_status"),
        "latest_status_remarks": p.get("latest_status_remarks"),
        "latest_match_score": p.get("latest_match_score"),
        "resume_versions_count": int(p.get("resume_versions_count") or 0),
        "latest_resume_version": p.get("latest_resume_version"),
        "resume": resume_summary,
        "recent_activity": recent_activity,
        "job_count": int(p.get("job_count") or 0),
        "pipeline": pipeline,
    }


_AMBIGUOUS_STAGE_TERMS = {
    "selected", "shortlisted", "best", "top", "good",
    "qualified", "approved", "final", "winning",
}


def _available_outcome_tags(ctx: ToolContext, job_id: Optional[int]) -> List[str]:
    """Distinct `pipeline_stage_status.tag` values reachable from the job's
    pipeline (or every pipeline if `job_id` is None).

    Tags are how the dashboard categorizes candidates across stages —
    e.g. SELECTED, OFFER_ACCEPTED, OFFER_RELEASED — so a user asking for
    "top candidates who are selected" really wants candidates whose
    current `candidate_pipeline_status.status` matches an option whose
    tag is one of these. We expose them in the elicitation form alongside
    stage names so the user can pick either.
    """
    if job_id is not None:
        rows = ctx.mcp.query(
            """
            SELECT DISTINCT pss.tag AS tag
              FROM pipeline_stage_status pss
              JOIN pipeline_stages ps ON ps.id = pss.pipeline_stage_id
              JOIN job_openings j ON j.pipeline_id = ps.pipeline_id
             WHERE j.id = :jid AND pss.tag IS NOT NULL
             ORDER BY pss.tag
            """,
            {"jid": job_id},
        )
    else:
        rows = ctx.mcp.query(
            "SELECT DISTINCT tag FROM pipeline_stage_status WHERE tag IS NOT NULL ORDER BY tag",
            {},
        )
    return [r["tag"] for r in rows if r.get("tag")]


def _available_stages(ctx: ToolContext, job_id: Optional[int]) -> List[str]:
    """Distinct stage names defined on the job's pipeline (or anywhere
    if `job_id` is None).

    The deployed schema tracks per-(candidate, job) stage in
    `candidate_pipeline_status (candidate_job_id, pipeline_stage_id,
    latest)` linked to `pipeline_stages (name, `order`, pipeline_id)` —
    NOT a `stage` column on `candidate_jobs`. We pull stages defined on
    the job's pipeline so the elicitation form can offer every legitimate
    option, including stages that don't have candidates yet.
    """
    if job_id is not None:
        rows = ctx.mcp.query(
            """
            SELECT ps.name AS stage,
                   ps.`order` AS stage_order,
                   ps.id AS stage_pk
              FROM pipeline_stages ps
              JOIN job_openings j ON j.pipeline_id = ps.pipeline_id
             WHERE j.id = :jid AND ps.name IS NOT NULL
             ORDER BY ps.`order`, ps.id
            """,
            {"jid": job_id},
        )
        if rows:
            return [r["stage"] for r in rows if r.get("stage")]
        # Fallback when the job has no pipeline_id yet — surface stages
        # that actually appear in the data for that job. Use GROUP BY so
        # ORDER BY references aggregate columns, sidestepping MySQL
        # ONLY_FULL_GROUP_BY mode which forbids DISTINCT + ORDER BY on a
        # column not in the select list.
        rows = ctx.mcp.query(
            """
            SELECT ps.name AS stage,
                   MIN(ps.`order`) AS stage_order,
                   MIN(ps.id) AS stage_pk
              FROM candidate_pipeline_status cps
              JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
              JOIN candidate_jobs cj ON cj.id = cps.candidate_job_id
             WHERE cj.job_id = :jid AND cps.latest = 1
             GROUP BY ps.name
             ORDER BY stage_order, stage_pk
            """,
            {"jid": job_id},
        )
        return [r["stage"] for r in rows if r.get("stage")]

    rows = ctx.mcp.query(
        """
        SELECT ps.name AS stage,
               MIN(ps.`order`) AS stage_order,
               MIN(ps.id) AS stage_pk
          FROM pipeline_stages ps
         WHERE ps.name IS NOT NULL
         GROUP BY ps.name
         ORDER BY stage_order, stage_pk
        """,
        {},
    )
    return [r["stage"] for r in rows if r.get("stage")]


def _stage_elicitation(
    stages: List[str],
    outcome_tags: Optional[List[str]] = None,
    note_extra: str = "",
) -> Dict[str, Any]:
    """Build a 'pick a stage or outcome' form when the user's intent is unclear.

    Stages come from `pipeline_stages.name` (the pipeline's columns).
    Outcome tags come from `pipeline_stage_status.tag` (cross-stage
    categories like SELECTED / OFFER_ACCEPTED / REJECTED). Both flow
    through the same select; the value uses a `outcome:` prefix for tag
    selections so `_list_candidates` can route the filter correctly.
    """
    options: List[ElicitationOption] = []
    for s in stages:
        options.append(ElicitationOption(
            value=s,
            label=s,
            description="Filter by pipeline stage",
        ))
    for t in outcome_tags or []:
        # Pretty-print the tag enum (e.g. OFFER_ACCEPTED → "Offer Accepted")
        pretty = t.replace("_", " ").title()
        options.append(ElicitationOption(
            value=f"outcome:{t}",
            label=f"{pretty} (any stage)",
            description="Filter by candidate outcome status",
        ))
    spec = ElicitationSpec(
        title="Which pipeline stage or outcome do you mean?",
        intro=(
            "I couldn't map your wording to a specific stage. Pick a stage "
            "name (filters by pipeline column) or an outcome tag (filters "
            "by accepted / rejected / offer-accepted across all stages)."
            + (f" {note_extra}" if note_extra else "")
        ),
        fields=[
            ElicitationField(
                name="stage",
                label="Stage or outcome",
                kind="select" if len(options) > 4 else "buttons",
                options=options,
                required=True,
            ),
        ],
        submit_label="Use this filter",
    )
    return make_elicitation(spec)


def _list_candidates(ctx: ToolContext, args: ListCandidatesArgs) -> Dict[str, Any]:
    where = ["1=1"]
    params: Dict[str, Any] = {}
    if args.job_id is not None:
        if not ctx.scope.has_job(args.job_id):
            return {"access_denied": True, "type": "job", "id": args.job_id}
        where.append("cj.job_id = :job_id")
        params["job_id"] = args.job_id

    # Server-side stage / outcome disambiguation. Stages live in
    # `pipeline_stages.name`; outcome tags live in
    # `pipeline_stage_status.tag` and matter when the user asks
    # "selected / accepted / rejected / offered" — those are
    # cross-stage categories, not stages.
    #   - Plain stage match → JOIN `pipeline_stages.name`
    #   - "outcome:<TAG>"   → JOIN `pipeline_stage_status` and filter
    #                          by `pss.tag = <TAG>` AND
    #                          UPPER(cps.status) = UPPER(pss.option)
    #   - Ambiguous wording → return elicitation form with both
    #                          stages AND outcome tags as options
    if args.stage:
        stage_input = args.stage.strip()
        if stage_input.lower().startswith("outcome:"):
            tag_value = stage_input.split(":", 1)[1].strip()
            where.append("pss.tag = :outcome_tag")
            where.append("UPPER(cps.status) = UPPER(pss.option)")
            params["outcome_tag"] = tag_value
        else:
            actual = _available_stages(ctx, args.job_id)
            actual_upper = {s.upper(): s for s in actual}
            if stage_input.upper() in actual_upper:
                params["stage"] = stage_input.upper()
                where.append("UPPER(ps.name) = :stage")
            elif (stage_input.lower() in _AMBIGUOUS_STAGE_TERMS) or actual:
                outcome_tags = _available_outcome_tags(ctx, args.job_id)
                return _stage_elicitation(
                    actual,
                    outcome_tags=outcome_tags,
                    note_extra=f"You wrote: '{stage_input}'.",
                )
            else:
                where.append("UPPER(ps.name) = :stage")
                params["stage"] = stage_input.upper()
    if args.date_from:
        where.append("cj.created_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.created_at <= :date_to")
        params["date_to"] = args.date_to

    scope_clause, scope_params = _scope_candidate_filter(ctx)
    params.update(scope_params)
    expanding = ["scope_cands"] if "scope_cands" in scope_params else None

    # When the filter is by outcome tag we need pipeline_stage_status in
    # the join chain. INNER joins on these tables would hide candidates
    # without a current status row, so we keep them LEFT joins for the
    # plain "no filter" / stage-name path and tighten with a WHERE NOT
    # NULL on cps.id only for outcome filtering.
    sql = f"""
        SELECT c.candidate_id AS id,
               c.candidate_name AS name,
               c.candidate_email AS email,
               ps.name AS stage,
               cps.status AS status_option,
               pss.tag AS outcome_tag,
               cj.created_at AS applied_at, cj.job_id,
               j.title AS job_title, j.job_id AS job_external_id
          FROM candidates c
          JOIN candidate_jobs cj ON cj.candidate_id = c.candidate_id
          LEFT JOIN job_openings j ON j.id = cj.job_id
          LEFT JOIN candidate_pipeline_status cps
                 ON cps.candidate_job_id = cj.id AND cps.latest = 1
          LEFT JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
          LEFT JOIN pipeline_stage_status pss
                 ON pss.pipeline_stage_id = cps.pipeline_stage_id
                AND UPPER(pss.option) = UPPER(cps.status)
         WHERE {' AND '.join(where)} {scope_clause}
         ORDER BY cj.created_at DESC
         LIMIT :_limit
    """
    params["_limit"] = args.limit
    rows = ctx.mcp.query(sql, params, expanding_keys=expanding)
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "name": (r.get("name") or r.get("email") or f"Candidate {r['id']}"),
            "email": r.get("email"),
            "stage": r.get("stage"),
            "status": r.get("status_option"),
            "outcome": r.get("outcome_tag"),
            "applied_at": str(r.get("applied_at")) if r.get("applied_at") else None,
            "job_id": r.get("job_id"),
            "job_title": r.get("job_title"),
        })
        ctx.add_output_ref({"type": "candidate", "id": r["id"]})
    return {"items": items, "count": len(items)}


def _pipeline_status_for_job(ctx: ToolContext, args: JobOnlyArgs) -> Dict[str, Any]:
    if not ctx.scope.has_job(args.job_id):
        return {"access_denied": True, "type": "job", "id": args.job_id}
    ctx.add_output_ref({"type": "job", "id": args.job_id})
    # Per-stage counts via the proper join through candidate_pipeline_status
    # (`latest=1` keeps only the candidate's current stage row). Stage
    # display names come from pipeline_stages.name; ordering follows the
    # pipeline's `order` column so the funnel reads top-to-bottom the
    # same way the dashboard funnel does.
    rows = ctx.mcp.query(
        """
        SELECT ps.id   AS stage_id,
               ps.name AS stage,
               ps.`order` AS stage_order,
               COUNT(*) AS cnt
          FROM candidate_pipeline_status cps
          JOIN candidate_jobs cj ON cj.id = cps.candidate_job_id
          JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
         WHERE cj.job_id = :jid
           AND cps.latest = 1
         GROUP BY ps.id, ps.name, ps.`order`
         ORDER BY ps.`order`, ps.id
        """,
        {"jid": args.job_id},
    )
    by_stage = [
        {"stage_id": r.get("stage_id"),
         "stage": r.get("stage") or "Unknown",
         "count": int(r.get("cnt") or 0)}
        for r in rows
    ]
    # Always include the total applicant count as a sanity number — some
    # rows may not yet have a candidate_pipeline_status (e.g. brand-new
    # applications), so total >= sum(by_stage).
    total_rows = ctx.mcp.query(
        "SELECT COUNT(*) AS cnt FROM candidate_jobs WHERE job_id = :jid",
        {"jid": args.job_id},
    )
    total_applicants = int(total_rows[0].get("cnt") or 0) if total_rows else 0
    return {
        "job_id": args.job_id,
        "by_stage": by_stage,
        "in_pipeline": sum(s["count"] for s in by_stage),
        "total_applicants": total_applicants,
    }


def _count_candidates_by_stage(ctx: ToolContext, args: CountByStageArgs) -> Dict[str, Any]:
    where = ["1=1"]
    params: Dict[str, Any] = {}
    if args.job_id is not None:
        if not ctx.scope.has_job(args.job_id):
            return {"access_denied": True, "type": "job", "id": args.job_id}
        where.append("cj.job_id = :job_id")
        params["job_id"] = args.job_id
    if args.company_id is not None:
        if not ctx.scope.has_company(args.company_id):
            return {"access_denied": True, "type": "company", "id": args.company_id}
        where.append("j.company_id = :company_id")
        params["company_id"] = args.company_id
    if args.date_from:
        where.append("cj.created_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.created_at <= :date_to")
        params["date_to"] = args.date_to

    scope_clause, scope_params = _scope_job_filter(ctx, alias="j")
    params.update(scope_params)
    expanding = ["scope_jobs"] if "scope_jobs" in scope_params else None

    sql = f"""
        SELECT ps.id   AS stage_id,
               ps.name AS stage,
               ps.`order` AS stage_order,
               COUNT(*) AS cnt
          FROM candidate_pipeline_status cps
          JOIN candidate_jobs cj ON cj.id = cps.candidate_job_id
          JOIN job_openings   j  ON j.id  = cj.job_id
          JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
         WHERE cps.latest = 1
           AND {' AND '.join(where)} {scope_clause}
         GROUP BY ps.id, ps.name, ps.`order`
         ORDER BY ps.`order`, ps.id
    """
    rows = ctx.mcp.query(sql, params, expanding_keys=expanding)
    return {
        "by_stage": [
            {"stage_id": r.get("stage_id"),
             "stage": r.get("stage") or "Unknown",
             "count": int(r.get("cnt") or 0)}
            for r in rows
        ],
    }


def _recruiter_metrics(ctx: ToolContext, args: RecruiterMetricsArgs) -> Dict[str, Any]:
    """Per-recruiter stage counts for the date window."""
    target_uid = args.user_id or ctx.user_id
    # Non-admins can only see themselves.
    if not ctx.scope.unscoped and target_uid != ctx.user_id:
        return {"access_denied": True, "type": "user", "id": target_uid}
    where_dt = ""
    params: Dict[str, Any] = {"uid": target_uid}
    if args.date_from:
        where_dt += " AND cj.created_at >= :date_from"
        params["date_from"] = args.date_from
    if args.date_to:
        where_dt += " AND cj.created_at <= :date_to"
        params["date_to"] = args.date_to
    ctx.add_output_ref({"type": "user", "id": target_uid})
    rows = ctx.mcp.query(
        f"""
        SELECT ps.id   AS stage_id,
               ps.name AS stage,
               ps.`order` AS stage_order,
               COUNT(*) AS cnt
          FROM candidate_pipeline_status cps
          JOIN candidate_jobs cj ON cj.id = cps.candidate_job_id
          JOIN user_jobs_assigned uja ON uja.job_id = cj.job_id
          JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
         WHERE uja.user_id = :uid
           AND cps.latest = 1
           {where_dt}
         GROUP BY ps.id, ps.name, ps.`order`
         ORDER BY ps.`order`, ps.id
        """,
        params,
    )
    by_stage = [
        {"stage_id": r.get("stage_id"),
         "stage": r.get("stage") or "Unknown",
         "count": int(r.get("cnt") or 0)}
        for r in rows
    ]
    return {"user_id": target_uid, "by_stage": by_stage,
            "total": sum(s["count"] for s in by_stage)}


def _top_recruiters(ctx: ToolContext, args: TopRecruitersArgs) -> Dict[str, Any]:
    if not ctx.scope.unscoped:
        # Non-admins don't get cross-recruiter leaderboards.
        return {"access_denied": True, "type": "leaderboard"}
    where = ""
    params: Dict[str, Any] = {"_limit": args.limit}
    if args.date_from:
        where += " AND cj.created_at >= :date_from"
        params["date_from"] = args.date_from
    if args.date_to:
        where += " AND cj.created_at <= :date_to"
        params["date_to"] = args.date_to
    rows = ctx.mcp.query(
        f"""
        SELECT u.id AS user_id, u.name, u.username,
               COUNT(*) AS total_moves
          FROM candidate_jobs cj
          JOIN user_jobs_assigned uja ON uja.job_id = cj.job_id
          JOIN users u ON u.id = uja.user_id
         WHERE 1=1 {where}
         GROUP BY u.id
         ORDER BY total_moves DESC
         LIMIT :_limit
        """,
        params,
    )
    items = []
    for r in rows:
        items.append({"user_id": r["user_id"], "name": r.get("name"),
                      "username": r.get("username"),
                      "total_moves": int(r.get("total_moves") or 0)})
        ctx.add_output_ref({"type": "user", "id": r["user_id"]})
    return {"items": items, "count": len(items)}


def _company_jobs_summary(ctx: ToolContext, args: CompanyJobsArgs) -> Dict[str, Any]:
    where = ""
    params: Dict[str, Any] = {"_limit": args.limit}
    if args.date_from:
        where += " AND j.created_at >= :date_from"
        params["date_from"] = args.date_from
    if args.date_to:
        where += " AND j.created_at <= :date_to"
        params["date_to"] = args.date_to
    scope_clause, scope_params = _scope_job_filter(ctx, alias="j")
    params.update(scope_params)
    expanding = ["scope_jobs"] if "scope_jobs" in scope_params else None
    rows = ctx.mcp.query(
        f"""
        SELECT co.id AS company_id, co.company_name,
               COUNT(j.id) AS jobs,
               SUM(CASE WHEN UPPER(j.status) = 'ACTIVE' THEN 1 ELSE 0 END) AS active_jobs
          FROM companies co
          JOIN job_openings j ON j.company_id = co.id
         WHERE 1=1 {where} {scope_clause}
         GROUP BY co.id
         ORDER BY jobs DESC
         LIMIT :_limit
        """,
        params,
        expanding_keys=expanding,
    )
    items = []
    for r in rows:
        items.append({
            "company_id": r["company_id"], "company_name": r.get("company_name"),
            "jobs": int(r.get("jobs") or 0),
            "active_jobs": int(r.get("active_jobs") or 0),
        })
        ctx.add_output_ref({"type": "company", "id": r["company_id"]})
    return {"items": items, "count": len(items)}


def _search_entities(ctx: ToolContext, args: SearchArgs) -> Dict[str, Any]:
    """Free-text fuzzy search across jobs, candidates, companies, users
    and teams. Returns one bucket per entity kind so the model can pick
    the right id (e.g. user_id) for the follow-up tool call."""
    q = f"%{args.query.strip()}%"
    params = {"q": q, "_limit": args.limit}
    job_scope, job_scope_p = _scope_job_filter(ctx)
    cand_scope, cand_scope_p = _scope_candidate_filter(ctx)
    expanding: List[str] = []
    if "scope_jobs" in job_scope_p:
        expanding.append("scope_jobs")
    if "scope_cands" in cand_scope_p:
        expanding.append("scope_cands")
    params.update(job_scope_p)
    params.update(cand_scope_p)
    job_rows = ctx.mcp.query(
        f"""
        SELECT j.id, j.title, co.company_name
          FROM job_openings j
     LEFT JOIN companies co ON co.id = j.company_id
         WHERE j.title LIKE :q {job_scope}
         LIMIT :_limit
        """,
        params, expanding_keys=expanding or None,
    )
    cand_rows = ctx.mcp.query(
        f"""
        SELECT c.candidate_id AS id,
               c.candidate_name AS name,
               c.candidate_email AS email
          FROM candidates c
         WHERE (c.candidate_name LIKE :q OR c.candidate_email LIKE :q)
               {cand_scope}
         LIMIT :_limit
        """,
        params, expanding_keys=expanding or None,
    )
    co_rows = ctx.mcp.query(
        """
        SELECT co.id, co.company_name
          FROM companies co
         WHERE co.company_name LIKE :q
         LIMIT :_limit
        """,
        {"q": q, "_limit": args.limit},
    )
    # Users — searchable by name / username / email so prompts like
    # "Tell me jobs assigned to Supriyo Chowdhury" can be resolved.
    user_rows = ctx.mcp.query(
        """
        SELECT u.id, u.name, u.username, u.email,
               COALESCE(r.name, '') AS role_name
          FROM users u
     LEFT JOIN roles r ON r.id = u.role_id
         WHERE u.deleted_at IS NULL
           AND (u.name LIKE :q OR u.username LIKE :q OR u.email LIKE :q)
         LIMIT :_limit
        """,
        {"q": q, "_limit": args.limit},
    )
    team_where = ["t.deleted_at IS NULL",
                  "(t.name LIKE :q OR t.description LIKE :q OR t.department LIKE :q)"]
    team_params: Dict[str, Any] = {"q": q, "_limit": args.limit}
    if not ctx.scope.unscoped:
        team_where.append(
            "t.id IN (SELECT team_id FROM team_members "
            "WHERE user_id = :scope_self_user_id)"
        )
        team_params["scope_self_user_id"] = ctx.user_id
    team_rows = ctx.mcp.query(
        f"""
        SELECT t.id, t.name, t.department, t.status
          FROM teams t
         WHERE {' AND '.join(team_where)}
         LIMIT :_limit
        """,
        team_params,
    )
    # Pipelines — searchable by display name OR public slug.
    pipeline_where = ["pl.deleted_at IS NULL",
                      "(pl.name LIKE :q OR pl.pipeline_id LIKE :q)"]
    pipeline_params: Dict[str, Any] = {"q": q, "_limit": args.limit}
    pipeline_expanding: List[str] = []
    if not ctx.scope.unscoped:
        if not ctx.scope.job_ids:
            pipeline_where.append("1 = 0")
        else:
            pipeline_where.append(
                "pl.id IN (SELECT j2.pipeline_id FROM job_openings j2 "
                "WHERE j2.id IN :scope_jobs AND j2.pipeline_id IS NOT NULL)"
            )
            pipeline_params["scope_jobs"] = list(ctx.scope.job_ids)
            pipeline_expanding.append("scope_jobs")
    pipeline_rows = ctx.mcp.query(
        f"""
        SELECT pl.id, pl.name, pl.pipeline_id AS slug
          FROM pipelines pl
         WHERE {' AND '.join(pipeline_where)}
         LIMIT :_limit
        """,
        pipeline_params,
        expanding_keys=pipeline_expanding or None,
    )
    jobs = [{"id": r["id"], "title": r.get("title"),
             "company_name": r.get("company_name")} for r in job_rows]
    candidates = [
        {"id": r["id"],
         "name": (r.get("name") or r.get("email") or f"Candidate {r['id']}"),
         "email": r.get("email")} for r in cand_rows
    ]
    companies = [{"id": r["id"], "name": r.get("company_name")}
                 for r in co_rows]
    users = [
        {"id": r["id"], "name": r.get("name"),
         "username": r.get("username"), "email": r.get("email"),
         "role": r.get("role_name") or None}
        for r in user_rows
    ]
    teams = [
        {"id": r["id"], "name": r.get("name"),
         "department": r.get("department"), "status": r.get("status")}
        for r in team_rows
    ]
    pipelines = [
        {"id": r["id"], "name": r.get("name"), "slug": r.get("slug")}
        for r in pipeline_rows
    ]

    full_payload = {
        "jobs": jobs, "candidates": candidates, "companies": companies,
        "users": users, "teams": teams, "pipelines": pipelines,
    }

    # Disambiguation flow — single tool call resolves the entity OR
    # surfaces a "pick one" form so the user clicks instead of retyping.
    if args.disambiguate_kind:
        bucket = full_payload.get(args.disambiguate_kind + "s") or []
        if len(bucket) == 0:
            return {
                "not_found": True,
                "kind": args.disambiguate_kind,
                "query": args.query,
                "results": full_payload,
                "note": (
                    f"No {args.disambiguate_kind} found matching "
                    f"'{args.query}'. Tell the user plainly; suggest they "
                    "double-check the spelling or tag the entity with the "
                    "+ button."
                ),
            }
        if len(bucket) == 1:
            row = bucket[0]
            return {
                "resolved": True,
                "kind": args.disambiguate_kind,
                "id": row.get("id"),
                "label": (
                    row.get("name") or row.get("title") or row.get("email")
                    or str(row.get("id"))
                ),
                "match": row,
                "results": full_payload,
            }
        # Multiple matches → fire an elicitation form so the user picks.
        # The picked value is the entity id; the answer lands as
        # `[elicit:<id>] {"selection": "<id>"}` on the next turn so the
        # model just feeds that id into the follow-up tool call.
        kind_label_map = {
            "job": "job", "candidate": "candidate", "company": "company",
            "user": "user", "team": "team", "pipeline": "pipeline",
        }
        kind_label = kind_label_map.get(args.disambiguate_kind, args.disambiguate_kind)
        options: List[ElicitationOption] = []
        for row in bucket[:10]:
            label = (
                row.get("name") or row.get("title")
                or row.get("email") or f"#{row.get('id')}"
            )
            desc_parts: List[str] = []
            if row.get("email"):
                desc_parts.append(str(row["email"]))
            if row.get("username") and row.get("username") != row.get("name"):
                desc_parts.append(f"@{row['username']}")
            if row.get("role"):
                desc_parts.append(f"role: {row['role']}")
            if row.get("company_name"):
                desc_parts.append(str(row["company_name"]))
            if row.get("slug"):
                desc_parts.append(f"slug: {row['slug']}")
            if row.get("department"):
                desc_parts.append(f"dept: {row['department']}")
            options.append(ElicitationOption(
                value=str(row.get("id")),
                label=str(label),
                description=" · ".join(desc_parts) or None,
            ))
        spec = ElicitationSpec(
            title=f"Which {kind_label} did you mean?",
            intro=(
                f"I found {len(bucket)} {kind_label}s matching "
                f"'{args.query}'. Pick the one you meant and I'll continue."
            ),
            fields=[
                ElicitationField(
                    name="selection",
                    label=kind_label.title(),
                    kind="select" if len(options) > 4 else "buttons",
                    options=options,
                    required=True,
                ),
            ],
            submit_label=f"Use this {kind_label}",
        )
        payload = make_elicitation(spec)
        # Carry the original payload so the model still has context.
        payload["results"] = full_payload
        return payload

    return full_payload


def _dashboard_data(ctx: ToolContext, args: DashboardArgs) -> Dict[str, Any]:
    """Convenience alias for `render_chart` — emits the same report ref so
    the FE renders the matching interactive dashboard chart inline. Kept
    so the model can pick either name without producing a no-op."""
    # Delegate to the chart tool so name-resolution + ref shape stay in
    # exactly one place.
    from app.ai_chat_layer.tools.chart_tools import (
        RenderChartArgs as _RCA, _render_chart,
    )
    return _render_chart(
        ctx,
        _RCA(
            chart_id=args.chart_id,
            date_from=args.date_from, date_to=args.date_to,
            company_id=args.company_id, job_id=args.job_id,
            user_id=args.user_id,
        ),
    )


# ---------------------------------------------------------------------------
# v2 — broader query surface: pipelines, users, teams, companies
# ---------------------------------------------------------------------------

def _pipeline_stages_for_job(ctx: ToolContext, args: JobOnlyArgs) -> Dict[str, Any]:
    """Every stage on a job's pipeline + the per-stage status options
    with their tag (Sourcing / Screening / LineUps / TurnUps / Selected /
    OfferReleased / OfferAccepted / etc.)."""
    if not ctx.scope.has_job(args.job_id):
        return {"access_denied": True, "type": "job", "id": args.job_id}
    stages = ctx.mcp.query(
        """
        SELECT ps.id        AS stage_id,
               ps.name      AS stage,
               ps.`order`   AS stage_order,
               ps.end_stage AS end_stage
          FROM pipeline_stages ps
          JOIN job_openings j ON j.pipeline_id = ps.pipeline_id
         WHERE j.id = :jid
         ORDER BY ps.`order`, ps.id
        """,
        {"jid": args.job_id},
    )
    options = ctx.mcp.query(
        """
        SELECT pss.pipeline_stage_id AS stage_id,
               pss.option            AS option_label,
               pss.tag               AS tag,
               pss.`order`           AS option_order
          FROM pipeline_stage_status pss
          JOIN pipeline_stages ps ON ps.id = pss.pipeline_stage_id
          JOIN job_openings j ON j.pipeline_id = ps.pipeline_id
         WHERE j.id = :jid
         ORDER BY pss.`order`, pss.id
        """,
        {"jid": args.job_id},
    )
    options_by_stage: Dict[int, List[Dict[str, Any]]] = {}
    for o in options:
        options_by_stage.setdefault(o["stage_id"], []).append({
            "option": o.get("option_label"),
            "tag": o.get("tag"),
        })
    items = [{
        "stage_id": s["stage_id"],
        "stage": s["stage"],
        "order": s.get("stage_order"),
        "end_stage": bool(s.get("end_stage")),
        "options": options_by_stage.get(s["stage_id"], []),
    } for s in stages]
    ctx.add_output_ref({"type": "job", "id": args.job_id})
    return {"job_id": args.job_id, "stages": items, "count": len(items)}


def _pipeline_funnel(ctx: ToolContext, args: FunnelArgs) -> Dict[str, Any]:
    """Tag-bucketed funnel: counts per `pipeline_stage_status.tag`
    (Sourcing / Screening / LineUps / TurnUps / Selected / OfferReleased
    / OfferAccepted) plus a sample of candidates per bucket. Scope can be
    a single job, company, user (recruiter), team, or global.

    Adds rejected (`candidate_job_status.type = 'rejected'`) and joined
    counts on top so the model can answer "how many got rejected?" too.
    """
    where = ["1=1"]
    params: Dict[str, Any] = {}
    if args.date_from:
        where.append("cj.created_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.created_at <= :date_to")
        params["date_to"] = args.date_to

    if args.scope == "job":
        if args.scope_id is None:
            return {"error": "scope=job requires scope_id"}
        if not ctx.scope.has_job(args.scope_id):
            return {"access_denied": True, "type": "job", "id": args.scope_id}
        where.append("cj.job_id = :scope_id")
        params["scope_id"] = args.scope_id
        ctx.add_output_ref({"type": "job", "id": args.scope_id})
    elif args.scope == "company":
        if args.scope_id is None:
            return {"error": "scope=company requires scope_id"}
        if not ctx.scope.has_company(args.scope_id):
            return {"access_denied": True, "type": "company", "id": args.scope_id}
        where.append("j.company_id = :scope_id")
        params["scope_id"] = args.scope_id
        ctx.add_output_ref({"type": "company", "id": args.scope_id})
    elif args.scope == "user":
        if args.scope_id is None:
            return {"error": "scope=user requires scope_id"}
        if not ctx.scope.unscoped and args.scope_id != ctx.user_id:
            return {"access_denied": True, "type": "user", "id": args.scope_id}
        where.append("uja.user_id = :scope_id")
        params["scope_id"] = args.scope_id
        ctx.add_output_ref({"type": "user", "id": args.scope_id})
    elif args.scope == "team":
        if args.scope_id is None:
            return {"error": "scope=team requires scope_id"}
        # Team scope: any candidate on a job assigned to a team member
        where.append(
            "uja.user_id IN (SELECT user_id FROM team_members WHERE team_id = :scope_id)"
        )
        params["scope_id"] = args.scope_id
        ctx.add_output_ref({"type": "team", "id": args.scope_id})
    else:  # global
        if not ctx.scope.unscoped:
            # Recruiters: scope to their own assignments.
            scope_clause, scope_params = _scope_job_filter(ctx, alias="j")
            params.update(scope_params)
            if scope_clause:
                where.append(scope_clause.lstrip(" AND "))

    join_uja = (
        "JOIN user_jobs_assigned uja ON uja.job_id = cj.job_id"
        if args.scope in ("user", "team")
        else "LEFT JOIN user_jobs_assigned uja ON uja.job_id = cj.job_id"
    )

    # Tag buckets via pipeline_stage_status.tag
    by_tag_rows = ctx.mcp.query(
        f"""
        SELECT pss.tag AS tag, COUNT(DISTINCT cj.id) AS cnt
          FROM candidate_jobs cj
          JOIN job_openings j ON j.id = cj.job_id
          {join_uja}
          JOIN candidate_pipeline_status cps
                ON cps.candidate_job_id = cj.id AND cps.latest = 1
          JOIN pipeline_stage_status pss
                ON pss.pipeline_stage_id = cps.pipeline_stage_id
               AND UPPER(pss.option) = UPPER(cps.status)
         WHERE {' AND '.join(where)}
         GROUP BY pss.tag
         ORDER BY cnt DESC
        """,
        params,
    )
    by_tag = [{"tag": r.get("tag") or "Unknown", "count": int(r.get("cnt") or 0)}
              for r in by_tag_rows]

    # Stage breakdown via pipeline_stages.name (ordered by pipeline `order`).
    by_stage_rows = ctx.mcp.query(
        f"""
        SELECT ps.id AS stage_id, ps.name AS stage,
               ps.`order` AS stage_order,
               COUNT(DISTINCT cj.id) AS cnt
          FROM candidate_jobs cj
          JOIN job_openings j ON j.id = cj.job_id
          {join_uja}
          JOIN candidate_pipeline_status cps
                ON cps.candidate_job_id = cj.id AND cps.latest = 1
          JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
         WHERE {' AND '.join(where)}
         GROUP BY ps.id, ps.name, ps.`order`
         ORDER BY ps.`order`, ps.id
        """,
        params,
    )
    by_stage = [{"stage_id": r.get("stage_id"), "stage": r.get("stage"),
                 "count": int(r.get("cnt") or 0)} for r in by_stage_rows]

    # Rejected / joined counts via candidate_job_status (separate signal
    # from pipeline_stage tags).
    rj_rows = ctx.mcp.query(
        f"""
        SELECT LOWER(cjs.type) AS type, COUNT(DISTINCT cj.id) AS cnt
          FROM candidate_jobs cj
          JOIN job_openings j ON j.id = cj.job_id
          {join_uja}
          JOIN candidate_job_status cjs ON cjs.candidate_job_id = cj.id
         WHERE {' AND '.join(where)}
         GROUP BY cjs.type
        """,
        params,
    )
    by_type = {r.get("type"): int(r.get("cnt") or 0) for r in rj_rows if r.get("type")}

    # Sample candidates per tag — top N for the model to cite.
    sample_rows = ctx.mcp.query(
        f"""
        SELECT pss.tag AS tag,
               c.candidate_id AS id,
               c.candidate_name AS name,
               c.candidate_email AS email,
               cj.created_at AS applied_at,
               j.id AS job_id, j.title AS job_title
          FROM candidate_jobs cj
          JOIN job_openings j ON j.id = cj.job_id
          {join_uja}
          JOIN candidates c ON c.candidate_id = cj.candidate_id
          JOIN candidate_pipeline_status cps
                ON cps.candidate_job_id = cj.id AND cps.latest = 1
          JOIN pipeline_stage_status pss
                ON pss.pipeline_stage_id = cps.pipeline_stage_id
               AND UPPER(pss.option) = UPPER(cps.status)
         WHERE {' AND '.join(where)}
         ORDER BY cj.created_at DESC
         LIMIT 80
        """,
        params,
    )
    samples_by_tag: Dict[str, List[Dict[str, Any]]] = {}
    for r in sample_rows:
        tag = r.get("tag") or "Unknown"
        bucket = samples_by_tag.setdefault(tag, [])
        if len(bucket) >= args.limit_per_tag:
            continue
        bucket.append({
            "candidate_id": r.get("id"),
            "name": r.get("name"),
            "email": r.get("email"),
            "job_id": r.get("job_id"),
            "job_title": r.get("job_title"),
            "applied_at": str(r.get("applied_at")) if r.get("applied_at") else None,
        })

    return {
        "scope": args.scope,
        "scope_id": args.scope_id,
        "by_tag": by_tag,
        "by_stage": by_stage,
        "by_type": {
            "rejected": by_type.get("rejected", 0),
            "joined": by_type.get("joined", 0),
            "dropped": by_type.get("dropped", 0),
        },
        "samples_by_tag": samples_by_tag,
        "total_candidates": sum(s["count"] for s in by_stage),
    }


def _users_for_job(ctx: ToolContext, args: JobOnlyArgs) -> Dict[str, Any]:
    """Recruiters / users assigned to a job via `user_jobs_assigned`."""
    if not ctx.scope.has_job(args.job_id):
        return {"access_denied": True, "type": "job", "id": args.job_id}
    rows = ctx.mcp.query(
        """
        SELECT u.id, u.name, u.username, u.email,
               COALESCE(r.name, '') AS role_name
          FROM user_jobs_assigned uja
          JOIN users u ON u.id = uja.user_id
     LEFT JOIN roles r ON r.id = u.role_id
         WHERE uja.job_id = :jid
         ORDER BY u.name
        """,
        {"jid": args.job_id},
    )
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "name": r.get("name"),
            "username": r.get("username"), "email": r.get("email"),
            "role": r.get("role_name") or None,
        })
        ctx.add_output_ref({"type": "user", "id": r["id"]})
    ctx.add_output_ref({"type": "job", "id": args.job_id})
    return {"job_id": args.job_id, "items": items, "count": len(items)}


def _team_detail(ctx: ToolContext, args: TeamArgs) -> Dict[str, Any]:
    """Team header + member roster (joined to `team_members`, `users`,
    `roles`) + jobs assigned to this team via `job_team_assignments`.
    Returns description, department, status, creator info."""
    team_rows = ctx.mcp.query(
        """
        SELECT t.id, t.name, t.description, t.department, t.status,
               t.created_at, t.deleted_at,
               cu.name AS created_by_name, cu.id AS created_by_id
          FROM teams t
     LEFT JOIN users cu ON cu.id = t.created_by
         WHERE t.id = :tid
         LIMIT 1
        """,
        {"tid": args.team_id},
    )
    if not team_rows:
        return {"not_found": True, "type": "team", "id": args.team_id}
    h = team_rows[0]
    member_rows = ctx.mcp.query(
        """
        SELECT u.id, u.name, u.username, u.email,
               COALESCE(r.name, '') AS role_name,
               tm.role_in_team, tm.assigned_at
          FROM team_members tm
          JOIN users u ON u.id = tm.user_id
     LEFT JOIN roles r ON r.id = u.role_id
         WHERE tm.team_id = :tid
         ORDER BY tm.role_in_team DESC, u.name
        """,
        {"tid": args.team_id},
    )
    members = []
    for r in member_rows:
        members.append({
            "id": r["id"], "name": r.get("name"),
            "username": r.get("username"), "email": r.get("email"),
            "role": r.get("role_name") or None,
            "role_in_team": r.get("role_in_team"),
            "assigned_at": (
                str(r.get("assigned_at")) if r.get("assigned_at") else None
            ),
        })
        ctx.add_output_ref({"type": "user", "id": r["id"]})

    job_rows = ctx.mcp.query(
        """
        SELECT j.id, j.title, j.status, j.openings,
               co.id AS company_id, co.company_name,
               jta.assigned_at,
               (SELECT COUNT(*) FROM candidate_jobs cj
                 WHERE cj.job_id = j.id) AS applicant_count
          FROM job_team_assignments jta
          JOIN job_openings j ON j.id = jta.job_id
     LEFT JOIN companies co ON co.id = j.company_id
         WHERE jta.team_id = :tid
         ORDER BY jta.assigned_at DESC
         LIMIT 50
        """,
        {"tid": args.team_id},
    )
    jobs = [
        {
            "id": r.get("id"),
            "title": r.get("title"),
            "status": (r.get("status") or "").upper() or None,
            "openings": r.get("openings"),
            "company_id": r.get("company_id"),
            "company_name": r.get("company_name"),
            "applicant_count": int(r.get("applicant_count") or 0),
            "assigned_at": (
                str(r.get("assigned_at")) if r.get("assigned_at") else None
            ),
        }
        for r in job_rows
    ]

    ctx.add_output_ref({"type": "team", "id": args.team_id})
    return {
        "id": h["id"],
        "name": h.get("name"),
        "description": h.get("description"),
        "department": h.get("department"),
        "status": h.get("status"),
        "is_active": (
            h.get("deleted_at") is None
            and (h.get("status") or "").lower() == "active"
        ),
        "created_at": (
            str(h.get("created_at")) if h.get("created_at") else None
        ),
        "deleted_at": (
            str(h.get("deleted_at")) if h.get("deleted_at") else None
        ),
        "created_by": (
            {"id": h.get("created_by_id"), "name": h.get("created_by_name")}
            if h.get("created_by_id") else None
        ),
        "members": members,
        "member_count": len(members),
        "jobs": jobs,
        "jobs_count": len(jobs),
    }


def _team_performance(ctx: ToolContext, args: TeamArgs) -> Dict[str, Any]:
    """Per-team aggregate funnel: union of every team member's assigned
    jobs' candidates, bucketed by tag + stage."""
    return _pipeline_funnel(
        ctx,
        FunnelArgs(scope="team", scope_id=args.team_id, limit_per_tag=5),
    )


def _company_detail(ctx: ToolContext, args: CompanyOnlyArgs) -> Dict[str, Any]:
    """Company header + jobs/applicant counts."""
    if not ctx.scope.has_company(args.company_id):
        return {"access_denied": True, "type": "company", "id": args.company_id}
    rows = ctx.mcp.query(
        """
        SELECT co.id, co.company_name,
               (SELECT COUNT(*) FROM job_openings j WHERE j.company_id = co.id) AS jobs,
               (SELECT COUNT(*) FROM job_openings j
                 WHERE j.company_id = co.id AND UPPER(j.status) = 'ACTIVE') AS active_jobs,
               (SELECT COUNT(*) FROM candidate_jobs cj
                  JOIN job_openings j ON j.id = cj.job_id
                 WHERE j.company_id = co.id) AS applicants
          FROM companies co
         WHERE co.id = :cid
         LIMIT 1
        """,
        {"cid": args.company_id},
    )
    if not rows:
        return {"not_found": True, "type": "company", "id": args.company_id}
    r = rows[0]
    ctx.add_output_ref({"type": "company", "id": r["id"]})
    return {
        "id": r["id"], "name": r.get("company_name"),
        "jobs": int(r.get("jobs") or 0),
        "active_jobs": int(r.get("active_jobs") or 0),
        "applicants": int(r.get("applicants") or 0),
    }


def _company_jobs(ctx: ToolContext, args: ListJobsArgs) -> Dict[str, Any]:
    """Convenience wrapper around list_jobs with company_id required.
    The plain `list_jobs` already supports company filtering; this exists
    so the model has a clearly-named tool for the common pattern."""
    if args.company_id is None:
        return {"error": "company_id is required"}
    return _list_jobs(ctx, args)


def _company_performance(ctx: ToolContext, args: CompanyOnlyArgs) -> Dict[str, Any]:
    return _pipeline_funnel(
        ctx,
        FunnelArgs(scope="company", scope_id=args.company_id, limit_per_tag=5),
    )


def _user_detail(ctx: ToolContext, args: UserArgs) -> Dict[str, Any]:
    """User profile (id / name / username / email / role) + their
    assigned jobs + their team memberships."""
    target_uid = args.user_id
    if not ctx.scope.unscoped and target_uid != ctx.user_id:
        return {"access_denied": True, "type": "user", "id": target_uid}
    rows = ctx.mcp.query(
        """
        SELECT u.id, u.name, u.username, u.email,
               COALESCE(r.name, '') AS role_name
          FROM users u
     LEFT JOIN roles r ON r.id = u.role_id
         WHERE u.id = :uid
         LIMIT 1
        """,
        {"uid": target_uid},
    )
    if not rows:
        return {"not_found": True, "type": "user", "id": target_uid}
    profile = rows[0]
    jobs = ctx.mcp.query(
        """
        SELECT j.id, j.title, j.status, j.openings,
               co.company_name AS company_name
          FROM user_jobs_assigned uja
          JOIN job_openings j ON j.id = uja.job_id
     LEFT JOIN companies co ON co.id = j.company_id
         WHERE uja.user_id = :uid
         ORDER BY j.created_at DESC
         LIMIT 25
        """,
        {"uid": target_uid},
    )
    teams = ctx.mcp.query(
        """
        SELECT t.id, t.name, tm.role_in_team
          FROM team_members tm
          JOIN teams t ON t.id = tm.team_id
         WHERE tm.user_id = :uid
         ORDER BY t.name
        """,
        {"uid": target_uid},
    )
    ctx.add_output_ref({"type": "user", "id": target_uid})
    return {
        "id": profile["id"], "name": profile.get("name"),
        "username": profile.get("username"), "email": profile.get("email"),
        "role": profile.get("role_name") or None,
        "jobs": [{"id": j["id"], "title": j.get("title"),
                  "status": (j.get("status") or "").upper() or None,
                  "openings": j.get("openings"),
                  "company_name": j.get("company_name")} for j in jobs],
        "teams": [{"id": t["id"], "name": t.get("name"),
                   "role_in_team": t.get("role_in_team")} for t in teams],
        "job_count": len(jobs),
        "team_count": len(teams),
    }


def _compare_users(ctx: ToolContext, args: UserCompareArgs) -> Dict[str, Any]:
    """Per-user funnel side-by-side. Each user gets a `_pipeline_funnel`
    pass under user-scope; the response is a table-friendly array."""
    if not ctx.scope.unscoped:
        return {"access_denied": True, "type": "user_compare"}
    out_users = []
    for uid in args.user_ids:
        funnel = _pipeline_funnel(
            ctx,
            FunnelArgs(scope="user", scope_id=uid,
                       date_from=args.date_from, date_to=args.date_to,
                       limit_per_tag=3),
        )
        if funnel.get("access_denied"):
            continue
        # Pull display name once for nicer output.
        u_rows = ctx.mcp.query(
            "SELECT id, name, username FROM users WHERE id = :uid LIMIT 1",
            {"uid": uid},
        )
        u = u_rows[0] if u_rows else {"id": uid, "name": None, "username": None}
        out_users.append({
            "user": {"id": u["id"], "name": u.get("name"),
                     "username": u.get("username")},
            "by_tag": funnel.get("by_tag", []),
            "by_stage": funnel.get("by_stage", []),
            "by_type": funnel.get("by_type", {}),
            "total_candidates": funnel.get("total_candidates", 0),
        })
    return {"users": out_users, "count": len(out_users)}


def _user_sourcing(ctx: ToolContext, args: UserSourcingArgs) -> Dict[str, Any]:
    """Candidates the user (recruiter) sourced — i.e. distinct candidates
    on jobs the user is assigned to within the date range. We approximate
    "sourced by" via `cj.created_at` + the user's job assignments.
    """
    if not ctx.scope.unscoped and args.user_id != ctx.user_id:
        return {"access_denied": True, "type": "user", "id": args.user_id}
    where = ["uja.user_id = :uid"]
    params: Dict[str, Any] = {"uid": args.user_id, "_limit": args.limit}
    if args.date_from:
        where.append("cj.created_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.created_at <= :date_to")
        params["date_to"] = args.date_to
    rows = ctx.mcp.query(
        f"""
        SELECT DISTINCT
               c.candidate_id AS id,
               c.candidate_name AS name,
               c.candidate_email AS email,
               cj.created_at AS applied_at,
               j.id AS job_id,
               j.title AS job_title
          FROM candidate_jobs cj
          JOIN user_jobs_assigned uja ON uja.job_id = cj.job_id
          JOIN candidates c ON c.candidate_id = cj.candidate_id
          JOIN job_openings j ON j.id = cj.job_id
         WHERE {' AND '.join(where)}
         ORDER BY cj.created_at DESC
         LIMIT :_limit
        """,
        params,
    )
    items = []
    for r in rows:
        items.append({
            "candidate_id": r["id"], "name": r.get("name"),
            "email": r.get("email"),
            "applied_at": str(r.get("applied_at")) if r.get("applied_at") else None,
            "job_id": r.get("job_id"), "job_title": r.get("job_title"),
        })
        ctx.add_output_ref({"type": "candidate", "id": r["id"]})
    ctx.add_output_ref({"type": "user", "id": args.user_id})
    return {"user_id": args.user_id, "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Resume + activity tools
# ---------------------------------------------------------------------------

def _latest_resumes(ctx: ToolContext, args: LatestResumesArgs) -> Dict[str, Any]:
    """Most recently parsed resumes the caller can see. By default returns
    the LATEST resume per candidate (one row per candidate). Pass
    `candidate_id` for every version of a single candidate, or `job_id`
    to scope to current applicants of a job."""
    where: List[str] = []
    params: Dict[str, Any] = {"_limit": args.limit}
    expanding: List[str] = []

    join_fragments = [
        "LEFT JOIN candidates c ON c.candidate_id = rp.candidate_id",
        "LEFT JOIN resume_matching rm "
        "ON rm.candidate_id = rp.candidate_id "
        "AND rm.resume_version = rp.resume_version",
    ]

    if args.candidate_id is not None:
        cand_id = str(args.candidate_id)
        if not ctx.scope.has_candidate(cand_id):
            return {"access_denied": True, "type": "candidate", "id": cand_id}
        where.append("rp.candidate_id = :cand_id")
        params["cand_id"] = cand_id
        # All versions for one candidate — no per-candidate latest filter.
    else:
        # Latest version per candidate.
        where.append(
            "rp.resume_version = (SELECT MAX(rp2.resume_version) "
            "FROM resume_personal_details rp2 "
            "WHERE rp2.candidate_id = rp.candidate_id)"
        )
        # ACL: recruiters only see their assigned candidates.
        if not ctx.scope.unscoped:
            if not ctx.scope.candidate_ids:
                where.append("1 = 0")
            else:
                where.append("rp.candidate_id IN :scope_cands")
                params["scope_cands"] = list(ctx.scope.candidate_ids)
                expanding.append("scope_cands")

    if args.job_id is not None:
        if not ctx.scope.has_job(args.job_id):
            return {"access_denied": True, "type": "job", "id": args.job_id}
        join_fragments.append(
            "JOIN candidate_jobs cj ON cj.candidate_id = rp.candidate_id"
        )
        where.append("cj.job_id = :job_id")
        params["job_id"] = args.job_id

    sql = f"""
        SELECT rp.resume_id, rp.candidate_id, rp.resume_version,
               rp.summary, rp.languages, rp.webpage, rp.created_at,
               c.candidate_name, c.candidate_email,
               c.current_location, c.current_company, c.experience,
               c.profile_source,
               rm.overall_match_score, rm.skills_match_percentage,
               rm.matched_skills, rm.missing_skills
          FROM resume_personal_details rp
          {" ".join(join_fragments)}
         WHERE {' AND '.join(where) if where else '1=1'}
         ORDER BY rp.created_at DESC, rp.resume_version DESC
         LIMIT :_limit
    """.strip()
    rows = ctx.mcp.query(sql, params, expanding_keys=expanding or None)
    items = []
    for r in rows:
        cid = r.get("candidate_id")
        if cid:
            ctx.add_output_ref({"type": "candidate", "id": cid})
        items.append({
            "resume_id": r.get("resume_id"),
            "candidate_id": cid,
            "candidate_name": r.get("candidate_name"),
            "candidate_email": r.get("candidate_email"),
            "current_location": r.get("current_location"),
            "current_company": r.get("current_company"),
            "experience": r.get("experience"),
            "profile_source": r.get("profile_source"),
            "version": r.get("resume_version"),
            "summary": r.get("summary"),
            "languages": r.get("languages"),
            "webpage": r.get("webpage"),
            "created_at": (
                str(r.get("created_at")) if r.get("created_at") else None
            ),
            "match": {
                "overall": r.get("overall_match_score"),
                "skills_pct": r.get("skills_match_percentage"),
                "matched_skills": r.get("matched_skills"),
                "missing_skills": r.get("missing_skills"),
            } if r.get("overall_match_score") is not None else None,
        })
    return {"items": items, "count": len(items)}


def _candidate_resume_detail(
    ctx: ToolContext, args: CandidateResumeDetailArgs
) -> Dict[str, Any]:
    """Full resume bundle for one (candidate, version): header, languages,
    qualifications, experiences, skills, projects, courses, achievements,
    platforms, file metadata, JD↔resume match. Defaults to latest version."""
    cand_id = str(args.candidate_id)
    if not ctx.scope.has_candidate(cand_id):
        return {"access_denied": True, "type": "candidate", "id": cand_id}

    if args.resume_version is None:
        ver_rows = ctx.mcp.query(
            "SELECT MAX(resume_version) AS v FROM resume_personal_details "
            "WHERE candidate_id = :cid",
            {"cid": cand_id},
        )
        version = ver_rows[0]["v"] if ver_rows and ver_rows[0]["v"] is not None else None
    else:
        version = int(args.resume_version)
    if version is None:
        return {"not_found": True, "candidate_id": cand_id, "reason": "no_resume"}

    header_rows = ctx.mcp.query(
        """
        SELECT resume_id, candidate_id, resume_version,
               name, email_id, ph_number, address, languages,
               summary, dob, webpage, personal_details, created_at
          FROM resume_personal_details
         WHERE candidate_id = :cid AND resume_version = :ver
         LIMIT 1
        """,
        {"cid": cand_id, "ver": version},
    )
    if not header_rows:
        return {"not_found": True, "candidate_id": cand_id, "version": version}
    h = header_rows[0]
    rid = h["resume_id"]

    quals = ctx.mcp.query(
        "SELECT degree, specialization, institute, degree_date, "
        "degree_location, grade FROM resume_qualifications "
        "WHERE resume_id = :rid ORDER BY degree_date DESC",
        {"rid": rid},
    )
    exps = ctx.mcp.query(
        "SELECT position, company, year_of_exp, start_date, end_date, "
        "description, mode_of_work, type_of_employment "
        "FROM resume_experiences WHERE resume_id = :rid "
        "ORDER BY start_date DESC",
        {"rid": rid},
    )
    skills_rows = ctx.mcp.query(
        "SELECT skill_category, skill_name FROM resume_skills "
        "WHERE resume_id = :rid",
        {"rid": rid},
    )
    projects = ctx.mcp.query(
        "SELECT project_name, project_desc, collaborators, project_date, "
        "project_duration, skills_used, project_type, project_link "
        "FROM resume_projects WHERE resume_id = :rid "
        "ORDER BY project_date DESC",
        {"rid": rid},
    )
    courses = ctx.mcp.query(
        "SELECT course_name, organization, date FROM resume_courses "
        "WHERE resume_id = :rid ORDER BY date DESC",
        {"rid": rid},
    )
    achievements = ctx.mcp.query(
        "SELECT achievement_name, achievement_description "
        "FROM resume_achievements WHERE resume_id = :rid",
        {"rid": rid},
    )
    platforms = ctx.mcp.query(
        "SELECT platform_name, platform_link FROM resume_platforms "
        "WHERE resume_id = :rid",
        {"rid": rid},
    )
    file_meta = ctx.mcp.query(
        "SELECT file_type, total_word_count, page FROM resume_file_metadata "
        "WHERE resume_id = :rid LIMIT 1",
        {"rid": rid},
    )
    matching = ctx.mcp.query(
        """
        SELECT overall_match_score, skills_match_percentage,
               qualification_match_percentage, experience_match_percentage,
               designation_match_percentage,
               preferred_qualification, current_qualification, qualification_analysis,
               preferred_designation, current_designation, designation_analysis,
               preferred_experience, current_experience, experience_analysis,
               preferred_skills, current_skills, matched_skills, missing_skills,
               extra_skills, overall_analysis, recommendation
          FROM resume_matching
         WHERE candidate_id = :cid AND resume_version = :ver
         LIMIT 1
        """,
        {"cid": cand_id, "ver": version},
    )

    grouped_skills: Dict[str, List[str]] = {"technical": [], "soft": [], "other": []}
    for s in skills_rows:
        cat = s.get("skill_category") or "other"
        if cat not in grouped_skills:
            grouped_skills[cat] = []
        if s.get("skill_name"):
            grouped_skills[cat].append(s["skill_name"])

    def _s(v):
        return str(v) if v is not None else None

    ctx.add_output_ref({"type": "candidate", "id": cand_id})

    return {
        "candidate_id": cand_id,
        "resume_id": rid,
        "version": version,
        "header": {
            "name": h.get("name"),
            "email": h.get("email_id"),
            "phone": h.get("ph_number"),
            "address": h.get("address"),
            "languages": h.get("languages"),
            "summary": h.get("summary"),
            "dob": h.get("dob"),
            "webpage": h.get("webpage"),
            "personal_details": h.get("personal_details"),
            "created_at": _s(h.get("created_at")),
        },
        "qualifications": [
            {
                "degree": q.get("degree"),
                "specialization": q.get("specialization"),
                "institute": q.get("institute"),
                "degree_date": _s(q.get("degree_date")),
                "location": q.get("degree_location"),
                "grade": q.get("grade"),
            } for q in quals
        ],
        "experiences": [
            {
                "position": e.get("position"),
                "company": e.get("company"),
                "year_of_exp": e.get("year_of_exp"),
                "start_date": _s(e.get("start_date")),
                "end_date": _s(e.get("end_date")),
                "description": e.get("description"),
                "mode_of_work": e.get("mode_of_work"),
                "type_of_employment": e.get("type_of_employment"),
            } for e in exps
        ],
        "skills": grouped_skills,
        "projects": [
            {
                "name": pr.get("project_name"),
                "description": pr.get("project_desc"),
                "collaborators": pr.get("collaborators"),
                "date": _s(pr.get("project_date")),
                "duration": pr.get("project_duration"),
                "skills_used": pr.get("skills_used"),
                "type": pr.get("project_type"),
                "link": pr.get("project_link"),
            } for pr in projects
        ],
        "courses": [
            {"name": c.get("course_name"),
             "organization": c.get("organization"),
             "date": _s(c.get("date"))}
            for c in courses
        ],
        "achievements": [
            {"name": a.get("achievement_name"),
             "description": a.get("achievement_description")}
            for a in achievements
        ],
        "platforms": [
            {"name": p.get("platform_name"), "link": p.get("platform_link")}
            for p in platforms
        ],
        "file_metadata": (
            {"file_type": file_meta[0].get("file_type"),
             "word_count": file_meta[0].get("total_word_count"),
             "pages": file_meta[0].get("page")}
            if file_meta else None
        ),
        "matching": matching[0] if matching else None,
    }


def _candidate_activity_timeline(
    ctx: ToolContext, args: CandidateActivityArgs
) -> Dict[str, Any]:
    """Reverse-chronological feed of `candidate_activity` rows for one
    candidate, with the actor's display name joined in. Optionally
    filterable by event type."""
    cand_id = str(args.candidate_id)
    if not ctx.scope.has_candidate(cand_id):
        return {"access_denied": True, "type": "candidate", "id": cand_id}

    where = ["ca.candidate_id = :cid"]
    params: Dict[str, Any] = {"cid": cand_id, "_limit": args.limit}
    if args.type:
        where.append("ca.type = :type")
        params["type"] = args.type

    rows = ctx.mcp.query(
        f"""
        SELECT ca.id, ca.type, ca.remark, ca.key_id, ca.created_at,
               u.id AS actor_id, u.name AS actor_name, u.username AS actor_username
          FROM candidate_activity ca
     LEFT JOIN users u ON u.id = ca.user_id
         WHERE {' AND '.join(where)}
         ORDER BY ca.created_at DESC, ca.id DESC
         LIMIT :_limit
        """,
        params,
    )
    items = [
        {
            "id": r.get("id"),
            "type": r.get("type"),
            "remark": r.get("remark"),
            "key_id": r.get("key_id"),
            "created_at": (
                str(r.get("created_at")) if r.get("created_at") else None
            ),
            "actor": {
                "id": r.get("actor_id"),
                "name": r.get("actor_name"),
                "username": r.get("actor_username"),
            } if r.get("actor_id") else None,
        }
        for r in rows
    ]
    ctx.add_output_ref({"type": "candidate", "id": cand_id})
    return {"candidate_id": cand_id, "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Pipeline tools
# ---------------------------------------------------------------------------

def _recent_activity_feed(
    ctx: ToolContext, args: RecentActivityFeedArgs
) -> Dict[str, Any]:
    """Reverse-chronological flat feed of candidate_activity events
    across the caller's scope. Supports a few common scopes (single
    candidate / job / company / recruiter / team / global) plus
    optional filters by event type, actor, and time window.
    """
    where: List[str] = ["1=1"]
    params: Dict[str, Any] = {"_limit": args.limit}
    expanding: List[str] = []

    if args.scope == "candidate":
        if args.scope_id is None:
            return {"error": "scope_id (candidate_id) required for scope='candidate'"}
        cand_id = str(args.scope_id)
        if not ctx.scope.has_candidate(cand_id):
            return {"access_denied": True, "type": "candidate", "id": cand_id}
        where.append("ca.candidate_id = :cand_id")
        params["cand_id"] = cand_id
    elif args.scope == "job":
        if args.scope_id is None:
            return {"error": "scope_id (job_id) required for scope='job'"}
        job_id = int(args.scope_id)
        if not ctx.scope.has_job(job_id):
            return {"access_denied": True, "type": "job", "id": job_id}
        where.append(
            "ca.candidate_id IN (SELECT cj2.candidate_id FROM candidate_jobs cj2 "
            "WHERE cj2.job_id = :job_id)"
        )
        params["job_id"] = job_id
    elif args.scope == "company":
        if args.scope_id is None:
            return {"error": "scope_id (company_id) required for scope='company'"}
        company_id = int(args.scope_id)
        if not ctx.scope.has_company(company_id):
            return {"access_denied": True, "type": "company", "id": company_id}
        where.append(
            "ca.candidate_id IN (SELECT cj2.candidate_id FROM candidate_jobs cj2 "
            "JOIN job_openings j2 ON j2.id = cj2.job_id "
            "WHERE j2.company_id = :company_id)"
        )
        params["company_id"] = company_id
    elif args.scope == "recruiter":
        if args.scope_id is None:
            return {"error": "scope_id (user_id) required for scope='recruiter'"}
        rid = int(args.scope_id)
        if not ctx.scope.unscoped and rid != ctx.user_id:
            return {"access_denied": True, "type": "user", "id": rid}
        where.append(
            "ca.candidate_id IN (SELECT cj2.candidate_id FROM candidate_jobs cj2 "
            "JOIN user_jobs_assigned uja2 ON uja2.job_id = cj2.job_id "
            "WHERE uja2.user_id = :recruiter_id)"
        )
        params["recruiter_id"] = rid
    elif args.scope == "team":
        if args.scope_id is None:
            return {"error": "scope_id (team_id) required for scope='team'"}
        tid = int(args.scope_id)
        if not ctx.scope.unscoped:
            owns = ctx.mcp.query(
                "SELECT 1 FROM team_members WHERE team_id = :tid AND user_id = :uid LIMIT 1",
                {"tid": tid, "uid": ctx.user_id},
            )
            if not owns:
                return {"access_denied": True, "type": "team", "id": tid}
        where.append(
            "ca.candidate_id IN (SELECT cj2.candidate_id FROM candidate_jobs cj2 "
            "JOIN user_jobs_assigned uja2 ON uja2.job_id = cj2.job_id "
            "JOIN team_members tm2 ON tm2.user_id = uja2.user_id "
            "WHERE tm2.team_id = :team_id)"
        )
        params["team_id"] = tid
    else:  # global
        # Recruiters: scope to candidates on their assigned jobs.
        if not ctx.scope.unscoped:
            if not ctx.scope.candidate_ids:
                return {"items": [], "count": 0}
            where.append("ca.candidate_id IN :scope_cands")
            params["scope_cands"] = list(ctx.scope.candidate_ids)
            expanding.append("scope_cands")

    if args.type:
        where.append("ca.type = :type")
        params["type"] = args.type
    if args.acted_by_user_id is not None:
        where.append("ca.user_id = :actor_id")
        params["actor_id"] = int(args.acted_by_user_id)
    if args.since_days:
        where.append(
            "ca.created_at >= DATE_SUB(NOW(), INTERVAL :since_days DAY)"
        )
        params["since_days"] = int(args.since_days)

    rows = ctx.mcp.query(
        f"""
        SELECT ca.id, ca.candidate_id, ca.type, ca.remark, ca.key_id,
               ca.created_at,
               u.id AS actor_id, u.name AS actor_name,
               u.username AS actor_username,
               c.candidate_name
          FROM candidate_activity ca
     LEFT JOIN users u ON u.id = ca.user_id
     LEFT JOIN candidates c ON c.candidate_id = ca.candidate_id
         WHERE {' AND '.join(where)}
         ORDER BY ca.created_at DESC, ca.id DESC
         LIMIT :_limit
        """,
        params,
        expanding_keys=expanding or None,
    )
    items = []
    for r in rows:
        items.append({
            "id": r.get("id"),
            "candidate_id": r.get("candidate_id"),
            "candidate_name": r.get("candidate_name"),
            "type": r.get("type"),
            "remark": r.get("remark"),
            "key_id": r.get("key_id"),
            "created_at": (
                str(r.get("created_at")) if r.get("created_at") else None
            ),
            "actor": (
                {"id": r.get("actor_id"),
                 "name": r.get("actor_name"),
                 "username": r.get("actor_username")}
                if r.get("actor_id") else None
            ),
        })
        if r.get("candidate_id"):
            ctx.add_output_ref({"type": "candidate", "id": r["candidate_id"]})
    return {"scope": args.scope, "scope_id": args.scope_id,
            "items": items, "count": len(items)}


def _list_pipelines(ctx: ToolContext, args: ListPipelinesArgs) -> Dict[str, Any]:
    """List pipeline templates with job / active job / applicant counts.

    ACL: recruiters only see pipelines that have at least one of their
    assigned jobs. Admins see every pipeline (alive + dormant unless
    `active_only` is False).
    """
    where: List[str] = ["1=1"]
    params: Dict[str, Any] = {"_limit": args.limit}
    expanding: List[str] = []

    if args.active_only:
        where.append("pl.deleted_at IS NULL")

    if not ctx.scope.unscoped:
        if not ctx.scope.job_ids:
            where.append("1 = 0")
        else:
            where.append(
                "pl.id IN (SELECT j2.pipeline_id FROM job_openings j2 "
                "WHERE j2.id IN :scope_jobs AND j2.pipeline_id IS NOT NULL)"
            )
            params["scope_jobs"] = list(ctx.scope.job_ids)
            expanding.append("scope_jobs")

    sql = f"""
        SELECT pl.id, pl.pipeline_id, pl.name, pl.remarks, pl.created_at,
               pl.deleted_at,
               (SELECT COUNT(*) FROM job_openings j
                 WHERE j.pipeline_id = pl.id) AS jobs_count,
               (SELECT COUNT(*) FROM job_openings j
                 WHERE j.pipeline_id = pl.id
                   AND UPPER(j.status) = 'ACTIVE') AS active_jobs_count,
               (SELECT COUNT(*) FROM candidate_jobs cj
                  JOIN job_openings j ON j.id = cj.job_id
                 WHERE j.pipeline_id = pl.id) AS applicants_count,
               (SELECT COUNT(*) FROM pipeline_stages ps
                 WHERE ps.pipeline_id = pl.id) AS stages_count
          FROM pipelines pl
         WHERE {' AND '.join(where)}
         ORDER BY pl.deleted_at IS NULL DESC, pl.name ASC
         LIMIT :_limit
    """.strip()
    rows = ctx.mcp.query(sql, params, expanding_keys=expanding or None)
    items = []
    for r in rows:
        items.append({
            "id": r.get("id"),
            "external_id": r.get("pipeline_id"),
            "name": r.get("name"),
            "remarks": r.get("remarks"),
            "created_at": (
                str(r.get("created_at")) if r.get("created_at") else None
            ),
            "deleted_at": (
                str(r.get("deleted_at")) if r.get("deleted_at") else None
            ),
            "is_active": r.get("deleted_at") is None,
            "jobs_count": int(r.get("jobs_count") or 0),
            "active_jobs_count": int(r.get("active_jobs_count") or 0),
            "applicants_count": int(r.get("applicants_count") or 0),
            "stages_count": int(r.get("stages_count") or 0),
        })
        ctx.add_output_ref({"type": "pipeline", "id": r["id"]})
    return {"items": items, "count": len(items)}


def _pipeline_detail(ctx: ToolContext, args: PipelineDetailArgs) -> Dict[str, Any]:
    """Full pipeline metadata: header + stages (with description, color,
    end_stage flag, status options + tags) + jobs using this pipeline.

    ACL: non-admins must have at least one assigned job that uses this
    pipeline; otherwise access_denied.
    """
    pid = args.pipeline_id
    if not ctx.scope.unscoped:
        owns = ctx.mcp.query(
            "SELECT 1 AS ok FROM job_openings j "
            "WHERE j.pipeline_id = :pid "
            "AND j.id IN :scope_jobs LIMIT 1",
            {"pid": pid, "scope_jobs": list(ctx.scope.job_ids) or [-1]},
            expanding_keys=["scope_jobs"],
        )
        if not owns:
            return {"access_denied": True, "type": "pipeline", "id": pid}

    head_rows = ctx.mcp.query(
        """
        SELECT pl.id, pl.pipeline_id, pl.name, pl.remarks,
               pl.created_at, pl.deleted_at,
               u.name AS created_by_name
          FROM pipelines pl
     LEFT JOIN users u ON u.id = pl.created_by
         WHERE pl.id = :pid
         LIMIT 1
        """,
        {"pid": pid},
    )
    if not head_rows:
        return {"not_found": True, "type": "pipeline", "id": pid}
    h = head_rows[0]

    stages = ctx.mcp.query(
        """
        SELECT ps.id, ps.name, ps.description, ps.color_code,
               ps.`order`, ps.end_stage
          FROM pipeline_stages ps
         WHERE ps.pipeline_id = :pid
         ORDER BY ps.`order` ASC, ps.id ASC
        """,
        {"pid": pid},
    )
    stage_ids = [s["id"] for s in stages]
    options_by_stage: Dict[int, List[Dict[str, Any]]] = {}
    if stage_ids:
        opt_rows = ctx.mcp.query(
            """
            SELECT pss.id, pss.pipeline_stage_id, pss.option, pss.tag,
                   pss.color_code, pss.`order`
              FROM pipeline_stage_status pss
             WHERE pss.pipeline_stage_id IN :stage_ids
             ORDER BY pss.pipeline_stage_id, pss.`order` ASC, pss.id ASC
            """,
            {"stage_ids": stage_ids},
            expanding_keys=["stage_ids"],
        )
        for o in opt_rows:
            options_by_stage.setdefault(o["pipeline_stage_id"], []).append({
                "id": o.get("id"),
                "option": o.get("option"),
                "tag": o.get("tag"),
                "color_code": o.get("color_code"),
                "order": o.get("order"),
            })

    jobs_using = ctx.mcp.query(
        """
        SELECT j.id, j.title, j.status, j.openings,
               co.id AS company_id, co.company_name,
               (SELECT COUNT(*) FROM candidate_jobs cj WHERE cj.job_id = j.id) AS applicant_count
          FROM job_openings j
     LEFT JOIN companies co ON co.id = j.company_id
         WHERE j.pipeline_id = :pid
         ORDER BY j.created_at DESC
         LIMIT 25
        """,
        {"pid": pid},
    )

    ctx.add_output_ref({"type": "pipeline", "id": pid})
    return {
        "id": h["id"],
        "external_id": h.get("pipeline_id"),
        "name": h.get("name"),
        "remarks": h.get("remarks"),
        "is_active": h.get("deleted_at") is None,
        "created_at": (
            str(h.get("created_at")) if h.get("created_at") else None
        ),
        "deleted_at": (
            str(h.get("deleted_at")) if h.get("deleted_at") else None
        ),
        "created_by": h.get("created_by_name"),
        "stages": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "description": s.get("description"),
                "color_code": s.get("color_code"),
                "order": s.get("order"),
                "end_stage": bool(s.get("end_stage")),
                "options": options_by_stage.get(s["id"], []),
            }
            for s in stages
        ],
        "stages_count": len(stages),
        "jobs": [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "status": (j.get("status") or "").upper() or None,
                "openings": j.get("openings"),
                "applicant_count": int(j.get("applicant_count") or 0),
                "company_id": j.get("company_id"),
                "company_name": j.get("company_name"),
            }
            for j in jobs_using
        ],
        "jobs_count": len(jobs_using),
    }


def _list_teams(ctx: ToolContext, args: ListTeamsArgs) -> Dict[str, Any]:
    """List teams with department, status, member_count, jobs_count.

    ACL: non-admins see only teams they're a member of.
    """
    where: List[str] = ["1=1"]
    params: Dict[str, Any] = {"_limit": args.limit}

    if not args.include_deleted:
        where.append("t.deleted_at IS NULL")
    if args.status:
        where.append("LOWER(t.status) = LOWER(:status)")
        params["status"] = args.status
    if args.department:
        where.append("LOWER(t.department) = LOWER(:department)")
        params["department"] = args.department
    if args.search:
        where.append("(t.name LIKE :search OR t.description LIKE :search)")
        params["search"] = f"%{args.search.strip()}%"
    if args.has_jobs is True:
        where.append(
            "EXISTS (SELECT 1 FROM job_team_assignments jta2 "
            "WHERE jta2.team_id = t.id)"
        )
    elif args.has_jobs is False:
        where.append(
            "NOT EXISTS (SELECT 1 FROM job_team_assignments jta2 "
            "WHERE jta2.team_id = t.id)"
        )
    if args.has_members is True:
        where.append(
            "EXISTS (SELECT 1 FROM team_members tm2 "
            "WHERE tm2.team_id = t.id)"
        )
    elif args.has_members is False:
        where.append(
            "NOT EXISTS (SELECT 1 FROM team_members tm2 "
            "WHERE tm2.team_id = t.id)"
        )
    if args.date_from:
        where.append("t.created_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("t.created_at <= :date_to")
        params["date_to"] = args.date_to

    if not ctx.scope.unscoped:
        where.append(
            "t.id IN (SELECT team_id FROM team_members "
            "WHERE user_id = :scope_self_user_id)"
        )
        params["scope_self_user_id"] = ctx.user_id

    sql = f"""
        SELECT t.id, t.name, t.description, t.department, t.status,
               t.created_at, t.deleted_at,
               (SELECT COUNT(*) FROM team_members tm
                 WHERE tm.team_id = t.id) AS member_count,
               (SELECT COUNT(*) FROM team_members tm
                 WHERE tm.team_id = t.id
                   AND LOWER(tm.role_in_team) = 'manager') AS manager_count,
               (SELECT COUNT(*) FROM job_team_assignments jta
                 WHERE jta.team_id = t.id) AS jobs_count
          FROM teams t
         WHERE {' AND '.join(where)}
         ORDER BY t.deleted_at IS NULL DESC,
                  LOWER(t.status) = 'active' DESC,
                  t.name ASC
         LIMIT :_limit
    """.strip()
    rows = ctx.mcp.query(sql, params)
    items = []
    for r in rows:
        items.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "description": r.get("description"),
            "department": r.get("department"),
            "status": r.get("status"),
            "is_active": (
                r.get("deleted_at") is None
                and (r.get("status") or "").lower() == "active"
            ),
            "deleted_at": (
                str(r.get("deleted_at")) if r.get("deleted_at") else None
            ),
            "created_at": (
                str(r.get("created_at")) if r.get("created_at") else None
            ),
            "member_count": int(r.get("member_count") or 0),
            "manager_count": int(r.get("manager_count") or 0),
            "jobs_count": int(r.get("jobs_count") or 0),
        })
        ctx.add_output_ref({"type": "team", "id": r["id"]})
    return {"items": items, "count": len(items)}


def _compare_teams(ctx: ToolContext, args: TeamCompareArgs) -> Dict[str, Any]:
    """Side-by-side funnel for 2-8 teams. Each team gets a `_pipeline_funnel`
    pass under team-scope; the response is a table-friendly array."""
    out_teams = []
    for tid in args.team_ids:
        # Header lookup (also enforces ACL via team membership for non-admins).
        if not ctx.scope.unscoped:
            owns = ctx.mcp.query(
                "SELECT 1 AS ok FROM team_members "
                "WHERE team_id = :tid AND user_id = :uid LIMIT 1",
                {"tid": tid, "uid": ctx.user_id},
            )
            if not owns:
                continue
        funnel = _pipeline_funnel(
            ctx,
            FunnelArgs(scope="team", scope_id=tid,
                       date_from=args.date_from, date_to=args.date_to,
                       limit_per_tag=3),
        )
        if funnel.get("access_denied"):
            continue
        head = ctx.mcp.query(
            "SELECT id, name, department, status FROM teams "
            "WHERE id = :tid LIMIT 1",
            {"tid": tid},
        )
        h = head[0] if head else {"id": tid, "name": None,
                                   "department": None, "status": None}
        out_teams.append({
            "team": {
                "id": h["id"],
                "name": h.get("name"),
                "department": h.get("department"),
                "status": h.get("status"),
            },
            "by_tag": funnel.get("by_tag", []),
            "by_stage": funnel.get("by_stage", []),
            "by_type": funnel.get("by_type", {}),
            "total_candidates": funnel.get("total_candidates", 0),
        })
    return {"teams": out_teams, "count": len(out_teams)}


def _list_users(ctx: ToolContext, args: ListUsersArgs) -> Dict[str, Any]:
    """List platform users with role + jobs_count + team_count.

    Admin-only by default; non-admins are restricted to themselves.
    """
    where: List[str] = ["1=1"]
    params: Dict[str, Any] = {"_limit": args.limit}

    if not args.include_deleted:
        where.append("u.deleted_at IS NULL")
    if args.enabled is True:
        where.append("u.enable = 1")
    elif args.enabled is False:
        where.append("u.enable = 0")
    if args.role:
        where.append("LOWER(r.name) = LOWER(:role)")
        params["role"] = args.role
    if args.team_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM team_members tm2 "
            "WHERE tm2.user_id = u.id AND tm2.team_id = :team_id)"
        )
        params["team_id"] = args.team_id
    if args.search:
        where.append(
            "(u.name LIKE :search OR u.username LIKE :search "
            "OR u.email LIKE :search)"
        )
        params["search"] = f"%{args.search.strip()}%"
    if args.date_from:
        where.append("u.created_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("u.created_at <= :date_to")
        params["date_to"] = args.date_to

    if not ctx.scope.unscoped:
        where.append("u.id = :scope_self_user_id")
        params["scope_self_user_id"] = ctx.user_id

    sql = f"""
        SELECT u.id, u.name, u.username, u.email, u.employee_id,
               u.enable, u.deleted_at, u.created_at,
               r.name AS role_name,
               (SELECT COUNT(*) FROM user_jobs_assigned uja
                 WHERE uja.user_id = u.id) AS jobs_count,
               (SELECT COUNT(*) FROM team_members tm
                 WHERE tm.user_id = u.id) AS teams_count
          FROM users u
     LEFT JOIN roles r ON r.id = u.role_id
         WHERE {' AND '.join(where)}
         ORDER BY u.deleted_at IS NULL DESC, u.enable DESC, u.name ASC
         LIMIT :_limit
    """.strip()
    rows = ctx.mcp.query(sql, params)
    items = []
    for r in rows:
        items.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "username": r.get("username"),
            "email": r.get("email"),
            "employee_id": r.get("employee_id"),
            "role": r.get("role_name"),
            "is_enabled": bool(r.get("enable")),
            "is_active": (r.get("deleted_at") is None) and bool(r.get("enable")),
            "deleted_at": (
                str(r.get("deleted_at")) if r.get("deleted_at") else None
            ),
            "created_at": (
                str(r.get("created_at")) if r.get("created_at") else None
            ),
            "jobs_count": int(r.get("jobs_count") or 0),
            "teams_count": int(r.get("teams_count") or 0),
        })
        ctx.add_output_ref({"type": "user", "id": r["id"]})
    return {"items": items, "count": len(items)}


def _candidate_stage_history(
    ctx: ToolContext, args: CandidateStageHistoryArgs
) -> Dict[str, Any]:
    """Every cps row for one candidate (optionally narrowed to one job),
    joined to pipeline_stages + users (for the actor name).
    """
    cand_id = str(args.candidate_id)
    if not ctx.scope.has_candidate(cand_id):
        return {"access_denied": True, "type": "candidate", "id": cand_id}

    where = ["cj.candidate_id = :cid"]
    params: Dict[str, Any] = {"cid": cand_id, "_limit": args.limit}
    if args.job_id is not None:
        if not ctx.scope.has_job(args.job_id):
            return {"access_denied": True, "type": "job", "id": args.job_id}
        where.append("cj.job_id = :job_id")
        params["job_id"] = args.job_id

    rows = ctx.mcp.query(
        f"""
        SELECT cps.id, cps.candidate_job_id, cps.pipeline_stage_id,
               cps.status, cps.latest, cps.created_at,
               ps.name AS stage_name, ps.`order` AS stage_order,
               ps.end_stage,
               cj.job_id AS job_id, j.title AS job_title,
               co.company_name,
               u.id AS actor_id, u.name AS actor_name,
               u.username AS actor_username,
               pss.tag AS outcome_tag
          FROM candidate_pipeline_status cps
          JOIN candidate_jobs cj ON cj.id = cps.candidate_job_id
     LEFT JOIN job_openings j ON j.id = cj.job_id
     LEFT JOIN companies co ON co.id = j.company_id
     LEFT JOIN pipeline_stages ps ON ps.id = cps.pipeline_stage_id
     LEFT JOIN users u ON u.id = cps.created_by
     LEFT JOIN pipeline_stage_status pss
            ON pss.pipeline_stage_id = cps.pipeline_stage_id
           AND UPPER(pss.option) = UPPER(cps.status)
         WHERE {' AND '.join(where)}
         ORDER BY cps.created_at DESC, cps.id DESC
         LIMIT :_limit
        """,
        params,
    )
    items = []
    for r in rows:
        items.append({
            "id": r.get("id"),
            "candidate_job_id": r.get("candidate_job_id"),
            "job_id": r.get("job_id"),
            "job_title": r.get("job_title"),
            "company_name": r.get("company_name"),
            "stage_id": r.get("pipeline_stage_id"),
            "stage": r.get("stage_name"),
            "stage_order": r.get("stage_order"),
            "end_stage": bool(r.get("end_stage")),
            "status": r.get("status"),
            "outcome_tag": r.get("outcome_tag"),
            "is_current": bool(r.get("latest")),
            "moved_at": (
                str(r.get("created_at")) if r.get("created_at") else None
            ),
            "actor": (
                {"id": r.get("actor_id"),
                 "name": r.get("actor_name"),
                 "username": r.get("actor_username")}
                if r.get("actor_id") else None
            ),
        })
    ctx.add_output_ref({"type": "candidate", "id": cand_id})
    return {"candidate_id": cand_id, "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# LangChain StructuredTool builder
# ---------------------------------------------------------------------------

def build_tools(ctx: ToolContext) -> List[Any]:
    """Wrap each function above as a LangChain StructuredTool bound to ctx."""
    try:
        from langchain.tools import StructuredTool  # type: ignore
    except ImportError:
        try:
            from langchain_core.tools import StructuredTool  # type: ignore
        except ImportError:
            logger.warning("LangChain not installed — tool registry empty")
            return []

    def _wrap(name: str, args_schema, fn, description: str):
        def _runner(**kwargs):
            args = args_schema(**kwargs) if kwargs else args_schema()
            return _timed(ctx, name, kwargs, lambda: fn(ctx, args))
        return StructuredTool.from_function(
            func=_runner,
            name=name,
            description=description,
            args_schema=args_schema,
        )

    return [
        _wrap("list_jobs", ListJobsArgs, _list_jobs,
              "List jobs the caller can see. Filter by status, company, date range."),
        _wrap("job_detail", JobOnlyArgs, _job_detail,
              "Detailed metadata for a single job by integer id, including applicant + recruiter counts."),
        _wrap("candidate_detail", CandidateOnlyArgs, _candidate_detail,
              ("Complete profile bundle for ONE candidate by candidate_id "
               "(string). Returns EVERY column from `candidates` (identity, "
               "contact, employment, location, salary, availability, "
               "demographics, skills, sourcing, ownership) plus the latest "
               "resume header (summary, languages, top skills bucketed "
               "into technical/soft/other), latest match score breakdown, "
               "the last 5 candidate_activity rows, and every "
               "candidate_jobs link with current stage / outcome tag / "
               "job header. Call this whenever the user tags a candidate "
               "or asks for any kind of profile / analysis / details "
               "about a specific candidate — one call, all context.")),
        _wrap("list_candidates", ListCandidatesArgs, _list_candidates,
              "List candidates, optionally filtered by job_id, stage, or applied date range."),
        _wrap("pipeline_status_for_job", JobOnlyArgs, _pipeline_status_for_job,
              "Stage-by-stage candidate counts for a single job."),
        _wrap("count_candidates_by_stage", CountByStageArgs, _count_candidates_by_stage,
              "Aggregate stage counts across the caller's pipelines, optionally narrowed by job, company, or date range."),
        _wrap("recruiter_metrics", RecruiterMetricsArgs, _recruiter_metrics,
              "Per-recruiter stage breakdown. Non-admins can only target themselves."),
        _wrap("top_recruiters", TopRecruitersArgs, _top_recruiters,
              "Leaderboard of recruiters by total stage moves. Admin/SuperAdmin only."),
        _wrap("company_jobs_summary", CompanyJobsArgs, _company_jobs_summary,
              "Companies the caller can see, ordered by total job openings, with active job counts."),
        _wrap("search_entities", SearchArgs, _search_entities,
              ("Fuzzy free-text search across jobs, candidates, companies, "
               "users, teams (matched by name + description + department), "
               "and pipelines. Returns six buckets — `jobs`, "
               "`candidates`, `companies`, `users`, `teams`, `pipelines`.\n\n"
               "**Disambiguation: pass `disambiguate_kind` whenever the "
               "user names a single entity that isn't tagged.** With it "
               "set, the tool routes the result for you:\n"
               "  * 1 match → `{resolved: true, id, label}` — use `id` "
               "    directly in the follow-up call.\n"
               "  * 2+ matches → `{elicitation_pending: true}` and a "
               "    pick-one form is shown to the user; STOP this turn "
               "    and wait for their selection (it arrives as "
               "    `[elicit:<id>] {selection: <id>}`).\n"
               "  * 0 matches → `{not_found: true}` — tell the user "
               "    plainly. NEVER claim someone 'is not a recruiter' "
               "    or 'doesn't exist' without trying this first.")),
        _wrap("dashboard_data", DashboardArgs, _dashboard_data,
              ("Alias for render_chart — embeds the matching dashboard chart "
               "inline by chart_id (e.g. pipeline-funnel, daily-trend, "
               "hiring-funnel). Prefer render_chart; this is kept only so the "
               "model can use either name and still produce a chart.")),

        # ── v2: pipelines / users / teams / companies ─────────────────
        _wrap("pipeline_stages_for_job", JobOnlyArgs, _pipeline_stages_for_job,
              ("Every pipeline stage configured on a job, including the "
               "stage `order`, `end_stage` flag, and the per-stage status "
               "options with their tag (Sourcing / Screening / LineUps / "
               "TurnUps / Selected / OfferReleased / OfferAccepted). Use "
               "this when the user asks about the pipeline structure of a "
               "job.")),
        _wrap("pipeline_funnel", FunnelArgs, _pipeline_funnel,
              ("Tag-bucketed pipeline funnel scoped by job / company / "
               "user / team / global. Returns counts per outcome tag "
               "(Sourcing, Screening, LineUps, TurnUps, Selected, "
               "OfferReleased, OfferAccepted), counts per stage in pipeline "
               "`order`, separate rejected / joined / dropped counts via "
               "candidate_job_status, plus a sample of up to "
               "`limit_per_tag` candidates per tag for citation. Use this "
               "for ANY question about how candidates flow through the "
               "pipeline at any scope.")),
        _wrap("users_for_job", JobOnlyArgs, _users_for_job,
              ("List the recruiters / users assigned to a job (rows from "
               "user_jobs_assigned joined to users + roles). Returns "
               "name / username / email / role for each.")),
        _wrap("team_detail", TeamArgs, _team_detail,
              ("Team header + complete member roster. Pulls from teams + "
               "team_members + users + roles. Each member's role_in_team "
               "is included so you can identify managers vs members.")),
        _wrap("team_performance", TeamArgs, _team_performance,
              ("Aggregate pipeline funnel for a team — i.e. every "
               "candidate on every job assigned to any team member. Same "
               "shape as pipeline_funnel(scope=team).")),
        _wrap("company_detail", CompanyOnlyArgs, _company_detail,
              ("Company header — name + total jobs + active jobs + total "
               "applicants. For per-job lists call `company_jobs`; for "
               "the funnel call `company_performance`.")),
        _wrap("company_jobs", ListJobsArgs, _company_jobs,
              ("List a company's jobs (requires company_id). Same fields "
               "as list_jobs, just clearly named for the common use case.")),
        _wrap("company_performance", CompanyOnlyArgs, _company_performance,
              ("Aggregate pipeline funnel for one company across all its "
               "jobs. Same shape as pipeline_funnel(scope=company).")),
        _wrap("user_detail", UserArgs, _user_detail,
              ("Profile for one user — name, username, email, role, plus "
               "their assigned jobs (up to 25 most recent) and team "
               "memberships. Non-admins can only target themselves.")),
        _wrap("compare_users", UserCompareArgs, _compare_users,
              ("Side-by-side performance table for 2-8 users. Each entry "
               "has the user's funnel by_tag / by_stage / rejected-joined "
               "counts within the date range. Admin / SuperAdmin only.")),
        _wrap("user_sourcing", UserSourcingArgs, _user_sourcing,
              ("Distinct candidates the user (recruiter) brought into the "
               "pipeline within the date range — i.e. candidates on any "
               "job they are assigned to. Returns candidate name / email "
               "/ applied_at / job_title.")),

        # ── resume + activity ────────────────────────────────────────
        _wrap("latest_resumes", LatestResumesArgs, _latest_resumes,
              ("Most recently parsed resumes the caller can see. Default "
               "= latest resume per candidate, ordered by created_at DESC. "
               "Optional `candidate_id` returns every version for that "
               "candidate; optional `job_id` restricts to current "
               "applicants of that job. Each row carries the resume "
               "summary, candidate header, languages, the latest JD↔"
               "resume match score + matched / missing skills. Use this "
               "for 'latest resumes' / 'newest profiles' / 'top match "
               "scores for job X' style asks.")),
        _wrap("candidate_resume_detail", CandidateResumeDetailArgs,
              _candidate_resume_detail,
              ("Full resume bundle for one (candidate, version): header, "
               "qualifications, work experiences, skills (technical / "
               "soft / other), projects, courses, achievements, platform "
               "links, file metadata, and the JD↔resume match analysis "
               "(matched / missing / extra skills, recommendation). Pass "
               "`resume_version` to target a specific version; omit for "
               "the latest. Call this for any deep resume question — "
               "skills, education, work history, match analysis, "
               "recommendation.")),
        _wrap("recent_activity_feed", RecentActivityFeedArgs,
              _recent_activity_feed,
              ("Reverse-chronological feed of candidate_activity events "
               "across the caller's scope. Pick `scope` ('candidate' / "
               "'job' / 'company' / 'recruiter' / 'team' / 'global') "
               "and pass `scope_id` (omit for global). Optional filters: "
               "`type` (general/pipeline/accepted/status/rejected), "
               "`acted_by_user_id` ('what did recruiter X do'), "
               "`since_days`. Returns each event with the candidate "
               "name + actor name. Use this for 'what changed today' / "
               "'recent activity in Acme' / 'what did Supriyo do this "
               "week'.")),
        _wrap("candidate_activity_timeline", CandidateActivityArgs,
              _candidate_activity_timeline,
              ("Reverse-chronological feed of candidate_activity rows for "
               "one candidate (general / pipeline / accepted / status / "
               "rejected events) with the actor's display name joined "
               "in. Optional `type` filters to one event class. Use this "
               "for 'history of X' / 'what happened with X' / 'who "
               "moved this candidate' style questions.")),

        # ── teams ───────────────────────────────────────────────────
        _wrap("list_teams", ListTeamsArgs, _list_teams,
              ("List teams with department / status / member_count / "
               "manager_count / jobs_count / is_active. Filter by "
               "`department`, `status`, `search` (name + description), "
               "`has_jobs` (True/False/None), `has_members` (True/False/"
               "None), date range on created_at, `include_deleted`. "
               "Non-admins only see teams they belong to. Use this for "
               "'list teams', 'teams without members', 'teams in "
               "Engineering', 'archived teams' etc.")),
        _wrap("compare_teams", TeamCompareArgs, _compare_teams,
              ("Side-by-side funnel for 2-8 teams. Each entry has the "
               "team's funnel by_tag / by_stage / rejected-joined-"
               "dropped counts within the date range. Non-admins are "
               "restricted to teams they belong to.")),

        # ── users ───────────────────────────────────────────────────
        _wrap("list_users", ListUsersArgs, _list_users,
              ("List platform users with role + jobs_count + teams_count "
               "+ is_active. Filter by `role` (super_admin / admin / "
               "user), `enabled` (defaults to True; pass null to include "
               "disabled), `team_id` (members of one team), `search` "
               "(substring on name/username/email), date range on "
               "created_at, and `include_deleted`. Admin-only by "
               "default — non-admins only see their own row. Use this "
               "for 'list recruiters', 'list admins', 'who's on team "
               "X', 'inactive users', 'new users this month' style "
               "questions.")),

        # ── pipelines ───────────────────────────────────────────────
        _wrap("list_pipelines", ListPipelinesArgs, _list_pipelines,
              ("List pipeline templates the caller can see. Each row has "
               "the pipeline header (name, public slug, remarks, "
               "is_active) plus jobs_count / active_jobs_count / "
               "applicants_count / stages_count. Recruiters only see "
               "pipelines used by their assigned jobs. Use this to "
               "answer 'list all pipelines' / 'how many pipelines' / "
               "'which pipelines are most used'.")),
        _wrap("pipeline_detail", PipelineDetailArgs, _pipeline_detail,
              ("Full metadata for ONE pipeline by integer id — header "
               "(name, slug, remarks, created_at, created_by, "
               "is_active), every stage in pipeline `order` with its "
               "description / color / end_stage flag, every status "
               "option per stage with the cross-stage tag (Sourcing / "
               "Screening / LineUps / TurnUps / Selected / "
               "OfferReleased / OfferAccepted), plus up to 25 jobs "
               "currently using this pipeline with their applicant "
               "counts. Call this for any deep pipeline question — "
               "stages, options, structure, jobs using it.")),
        _wrap("candidate_stage_history", CandidateStageHistoryArgs,
              _candidate_stage_history,
              ("Every candidate_pipeline_status row for one candidate "
               "(optionally narrowed to a single job_id) joined to "
               "stage name + actor (who moved them) + outcome tag, "
               "ordered newest first. Each row carries `is_current` for "
               "the current stage. Use this for 'stage history of X' / "
               "'who moved them and when' / 'when did they reach "
               "Selected'.")),
    ]
