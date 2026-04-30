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
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

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


class DashboardArgs(DateRange):
    chart_id: str
    company_id: Optional[int] = None
    job_id: Optional[int] = None
    user_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

def _list_jobs(ctx: ToolContext, args: ListJobsArgs) -> Dict[str, Any]:
    scope_clause, scope_params = _scope_job_filter(ctx)
    where = ["j.deleted_at IS NULL"] if False else []
    params: Dict[str, Any] = {}
    if args.status:
        where.append("UPPER(j.status) = :status")
        params["status"] = args.status.upper()
    if args.company_id is not None:
        where.append("j.company_id = :company_id")
        params["company_id"] = args.company_id
    if args.date_from:
        where.append("j.created_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("j.created_at <= :date_to")
        params["date_to"] = args.date_to
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT j.id, j.job_id, j.title, j.status, j.openings,
               j.location, j.created_at,
               co.company_name AS company_name, co.id AS company_id
          FROM job_openings j
     LEFT JOIN companies co ON co.id = j.company_id
        {where_sql} {scope_clause.replace(' AND ', ' AND ', 1) if not where_sql else scope_clause}
         ORDER BY j.created_at DESC
         LIMIT :_limit
    """.strip()
    if not where_sql and scope_clause:
        sql = sql.replace("AND", "WHERE", 1)
    params.update(scope_params)
    params["_limit"] = args.limit
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
               co.id AS company_id, co.company_name,
               (SELECT COUNT(*) FROM candidate_jobs cj WHERE cj.job_id = j.id) AS applicant_count,
               (SELECT COUNT(*) FROM user_jobs_assigned uja WHERE uja.job_id = j.id) AS recruiter_count
          FROM job_openings j
     LEFT JOIN companies co ON co.id = j.company_id
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
        "applicant_count": r.get("applicant_count"),
        "recruiter_count": r.get("recruiter_count"),
        "created_at": str(r.get("created_at")) if r.get("created_at") else None,
    }


_AMBIGUOUS_STAGE_TERMS = {
    "selected", "shortlisted", "best", "top", "good",
    "qualified", "approved", "final", "winning",
}


def _available_stages(ctx: ToolContext, job_id: Optional[int]) -> List[str]:
    """Distinct stages currently on `candidate_jobs` (optionally for one job)."""
    if job_id is not None:
        rows = ctx.mcp.query(
            "SELECT DISTINCT stage FROM candidate_jobs "
            "WHERE job_id = :jid AND stage IS NOT NULL ORDER BY stage",
            {"jid": job_id},
        )
    else:
        rows = ctx.mcp.query(
            "SELECT DISTINCT stage FROM candidate_jobs "
            "WHERE stage IS NOT NULL ORDER BY stage",
            {},
        )
    return [r["stage"] for r in rows if r.get("stage")]


def _stage_elicitation(stages: List[str], note_extra: str = "") -> Dict[str, Any]:
    """Build the standard 'pick a stage' form when the user's intent is unclear."""
    options = [
        ElicitationOption(value=s, label=s) for s in stages
    ]
    spec = ElicitationSpec(
        title="Which pipeline stage do you mean?",
        intro=(
            "I couldn't map your wording to one of the actual stages on this "
            "pipeline. Pick the one you meant and I'll continue."
            + (f" {note_extra}" if note_extra else "")
        ),
        fields=[
            ElicitationField(
                name="stage",
                label="Stage",
                kind="select" if len(options) > 4 else "buttons",
                options=options,
                required=True,
            ),
        ],
        submit_label="Use this stage",
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

    # Server-side stage disambiguation. If the model passed a stage hint
    # that doesn't match any real value in the data (or is one of the
    # vague synonyms users commonly type), surface an elicitation form
    # instead of returning an empty list silently.
    if args.stage:
        stage_input = args.stage.strip()
        actual = _available_stages(ctx, args.job_id)
        actual_upper = {s.upper(): s for s in actual}
        if stage_input.upper() in actual_upper:
            # Normalize to the canonical casing used in the DB.
            params["stage"] = stage_input.upper()
            where.append("UPPER(cj.stage) = :stage")
        elif (stage_input.lower() in _AMBIGUOUS_STAGE_TERMS) or actual:
            return _stage_elicitation(
                actual,
                note_extra=f"You wrote: '{stage_input}'.",
            )
        # If `actual` is empty (no candidates yet for this job) we just
        # let the query run with the user's literal stage and return [].
        else:
            where.append("UPPER(cj.stage) = :stage")
            params["stage"] = stage_input.upper()
    if args.date_from:
        where.append("cj.applied_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.applied_at <= :date_to")
        params["date_to"] = args.date_to

    scope_clause, scope_params = _scope_candidate_filter(ctx)
    params.update(scope_params)
    expanding = ["scope_cands"] if "scope_cands" in scope_params else None

    sql = f"""
        SELECT c.candidate_id AS id,
               c.candidate_name AS name,
               c.candidate_email AS email,
               cj.stage, cj.applied_at, cj.job_id,
               j.title AS job_title, j.job_id AS job_external_id
          FROM candidates c
          JOIN candidate_jobs cj ON cj.candidate_id = c.candidate_id
          LEFT JOIN job_openings j ON j.id = cj.job_id
         WHERE {' AND '.join(where)} {scope_clause}
         ORDER BY cj.applied_at DESC
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
            "applied_at": str(r.get("applied_at")) if r.get("applied_at") else None,
            "job_id": r.get("job_id"),
            "job_title": r.get("job_title"),
        })
        ctx.add_output_ref({"type": "candidate", "id": r["id"]})
    return {"items": items, "count": len(items)}


def _pipeline_status_for_job(ctx: ToolContext, args: JobOnlyArgs) -> Dict[str, Any]:
    if not ctx.scope.has_job(args.job_id):
        return {"access_denied": True, "type": "job", "id": args.job_id}
    rows = ctx.mcp.query(
        """
        SELECT cj.stage AS stage, COUNT(*) AS cnt
          FROM candidate_jobs cj
         WHERE cj.job_id = :jid
         GROUP BY cj.stage
         ORDER BY cnt DESC
        """,
        {"jid": args.job_id},
    )
    by_stage = [{"stage": r.get("stage") or "Unknown", "count": int(r.get("cnt") or 0)}
                for r in rows]
    ctx.add_output_ref({"type": "job", "id": args.job_id})
    return {"job_id": args.job_id, "by_stage": by_stage,
            "total": sum(s["count"] for s in by_stage)}


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
        where.append("cj.applied_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.applied_at <= :date_to")
        params["date_to"] = args.date_to

    scope_clause, scope_params = _scope_job_filter(ctx, alias="j")
    params.update(scope_params)
    expanding = ["scope_jobs"] if "scope_jobs" in scope_params else None

    sql = f"""
        SELECT cj.stage AS stage, COUNT(*) AS cnt
          FROM candidate_jobs cj
          JOIN job_openings j ON j.id = cj.job_id
         WHERE {' AND '.join(where)} {scope_clause}
         GROUP BY cj.stage
         ORDER BY cnt DESC
    """
    rows = ctx.mcp.query(sql, params, expanding_keys=expanding)
    return {
        "by_stage": [
            {"stage": r.get("stage") or "Unknown", "count": int(r.get("cnt") or 0)}
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
        where_dt += " AND cj.applied_at >= :date_from"
        params["date_from"] = args.date_from
    if args.date_to:
        where_dt += " AND cj.applied_at <= :date_to"
        params["date_to"] = args.date_to
    rows = ctx.mcp.query(
        f"""
        SELECT cj.stage AS stage, COUNT(*) AS cnt
          FROM candidate_jobs cj
          JOIN user_jobs_assigned uja ON uja.job_id = cj.job_id
         WHERE uja.user_id = :uid
           {where_dt}
         GROUP BY cj.stage
         ORDER BY cnt DESC
        """,
        params,
    )
    by_stage = [{"stage": r.get("stage") or "Unknown", "count": int(r.get("cnt") or 0)}
                for r in rows]
    ctx.add_output_ref({"type": "user", "id": target_uid})
    return {"user_id": target_uid, "by_stage": by_stage,
            "total": sum(s["count"] for s in by_stage)}


def _top_recruiters(ctx: ToolContext, args: TopRecruitersArgs) -> Dict[str, Any]:
    if not ctx.scope.unscoped:
        # Non-admins don't get cross-recruiter leaderboards.
        return {"access_denied": True, "type": "leaderboard"}
    where = ""
    params: Dict[str, Any] = {"_limit": args.limit}
    if args.date_from:
        where += " AND cj.applied_at >= :date_from"
        params["date_from"] = args.date_from
    if args.date_to:
        where += " AND cj.applied_at <= :date_to"
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
    """Free-text fuzzy search across job titles + candidate names + companies."""
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
    return {
        "jobs": [{"id": r["id"], "title": r.get("title"),
                  "company_name": r.get("company_name")} for r in job_rows],
        "candidates": [
            {"id": r["id"],
             "name": (r.get("name") or r.get("email") or f"Candidate {r['id']}"),
             "email": r.get("email")} for r in cand_rows
        ],
        "companies": [{"id": r["id"], "name": r.get("company_name")} for r in co_rows],
    }


def _dashboard_data(ctx: ToolContext, args: DashboardArgs) -> Dict[str, Any]:
    """Convenience alias for `render_chart` — emits the same report ref so
    the FE renders the matching interactive dashboard chart inline. Kept
    so the model can pick either name without producing a no-op."""
    params = {
        "date_from": args.date_from, "date_to": args.date_to,
        "company_id": args.company_id, "job_id": args.job_id,
        "user_id": args.user_id,
    }
    params = {k: v for k, v in params.items() if v is not None}
    ref = {
        "type": "report",
        "id": args.chart_id,
        "title": args.chart_id.replace("-", " ").title(),
        "params": params,
    }
    ctx.add_output_ref(ref)
    return {"rendered": True, "chart_id": args.chart_id, "params": params,
            "ref": ref, "interactive": True}


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
              "Fuzzy free-text search across jobs, candidates, and companies."),
        _wrap("dashboard_data", DashboardArgs, _dashboard_data,
              ("Alias for render_chart — embeds the matching dashboard chart "
               "inline by chart_id (e.g. pipeline-funnel, daily-trend, "
               "hiring-funnel). Prefer render_chart; this is kept only so the "
               "model can use either name and still produce a chart.")),
    ]
