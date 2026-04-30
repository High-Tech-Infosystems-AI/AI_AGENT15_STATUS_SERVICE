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
        Literal["job", "candidate", "company", "user", "team"]
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


def _candidate_detail(ctx: ToolContext, args: CandidateOnlyArgs) -> Dict[str, Any]:
    """Full candidate profile from the `candidates` table plus their job
    pipeline links (one row per candidate_jobs, with current stage and
    outcome tag from candidate_pipeline_status / pipeline_stage_status).

    Mirrors the dashboard / job-service convention — the chatbot asked
    only `candidate_jobs` rows before, which lacked profile data; this
    tool returns the full row from `candidates` so the model can answer
    questions like "what's their experience?" or "where are they based?".
    """
    cand_id = str(args.candidate_id)
    if not ctx.scope.has_candidate(cand_id):
        return {"access_denied": True, "type": "candidate", "id": cand_id}

    rows = ctx.mcp.query(
        """
        SELECT c.candidate_id   AS id,
               c.candidate_name AS name,
               c.candidate_email AS email,
               c.employment_status,
               c.experience,
               c.current_company,
               c.current_location,
               c.job_profile,
               (SELECT cs.candidate_status
                  FROM candidate_status cs
                 WHERE cs.candidate_id = c.candidate_id
                 ORDER BY cs.updated_at DESC, cs.id DESC
                 LIMIT 1) AS latest_status,
               (SELECT COUNT(*) FROM candidate_jobs cj
                 WHERE cj.candidate_id = c.candidate_id) AS job_count
          FROM candidates c
         WHERE c.candidate_id = :cid
         LIMIT 1
        """,
        {"cid": cand_id},
    )
    if not rows:
        return {"not_found": True, "type": "candidate", "id": cand_id}
    p = rows[0]

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
               cj.applied_at,
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
         ORDER BY cj.applied_at DESC
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

    return {
        "id": p["id"],
        "name": p.get("name"),
        "email": p.get("email"),
        "employment_status": p.get("employment_status"),
        "experience_years": p.get("experience"),
        "current_company": p.get("current_company"),
        "current_location": p.get("current_location"),
        "job_profile": p.get("job_profile"),
        "latest_status": p.get("latest_status"),
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
        where.append("cj.applied_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.applied_at <= :date_to")
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
               cj.applied_at, cj.job_id,
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
        where.append("cj.applied_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.applied_at <= :date_to")
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
        where_dt += " AND cj.applied_at >= :date_from"
        params["date_from"] = args.date_from
    if args.date_to:
        where_dt += " AND cj.applied_at <= :date_to"
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
    team_rows = ctx.mcp.query(
        """
        SELECT t.id, t.name
          FROM teams t
         WHERE t.name LIKE :q
         LIMIT :_limit
        """,
        {"q": q, "_limit": args.limit},
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
    teams = [{"id": r["id"], "name": r.get("name")} for r in team_rows]

    full_payload = {
        "jobs": jobs, "candidates": candidates, "companies": companies,
        "users": users, "teams": teams,
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
            "user": "user", "team": "team",
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
        where.append("cj.applied_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.applied_at <= :date_to")
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
               cj.applied_at,
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
         ORDER BY cj.applied_at DESC
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
    `roles`). Mirrors what RBAC exposes on its team detail endpoint."""
    team_rows = ctx.mcp.query(
        "SELECT id, name FROM teams WHERE id = :tid LIMIT 1",
        {"tid": args.team_id},
    )
    if not team_rows:
        return {"not_found": True, "type": "team", "id": args.team_id}
    member_rows = ctx.mcp.query(
        """
        SELECT u.id, u.name, u.username, u.email,
               COALESCE(r.name, '') AS role_name,
               tm.role_in_team
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
        })
        ctx.add_output_ref({"type": "user", "id": r["id"]})
    ctx.add_output_ref({"type": "team", "id": args.team_id})
    return {
        "id": team_rows[0]["id"],
        "name": team_rows[0].get("name"),
        "members": members,
        "member_count": len(members),
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
    "sourced by" via `cj.applied_at` + the user's job assignments.
    """
    if not ctx.scope.unscoped and args.user_id != ctx.user_id:
        return {"access_denied": True, "type": "user", "id": args.user_id}
    where = ["uja.user_id = :uid"]
    params: Dict[str, Any] = {"uid": args.user_id, "_limit": args.limit}
    if args.date_from:
        where.append("cj.applied_at >= :date_from")
        params["date_from"] = args.date_from
    if args.date_to:
        where.append("cj.applied_at <= :date_to")
        params["date_to"] = args.date_to
    rows = ctx.mcp.query(
        f"""
        SELECT DISTINCT
               c.candidate_id AS id,
               c.candidate_name AS name,
               c.candidate_email AS email,
               cj.applied_at,
               j.id AS job_id,
               j.title AS job_title
          FROM candidate_jobs cj
          JOIN user_jobs_assigned uja ON uja.job_id = cj.job_id
          JOIN candidates c ON c.candidate_id = cj.candidate_id
          JOIN job_openings j ON j.id = cj.job_id
         WHERE {' AND '.join(where)}
         ORDER BY cj.applied_at DESC
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
              ("Full profile for ONE candidate by candidate_id (string). "
               "Returns name / email / experience / current company / "
               "current location / job profile / latest free-form status, "
               "plus every (candidate_jobs) link they have with the "
               "current pipeline stage + outcome tag and the job's "
               "title/status/company. Call this whenever the user tags "
               "a candidate or asks for details / profile / experience / "
               "where-they-are about a specific candidate.")),
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
               "users and teams. Returns five buckets — `jobs`, "
               "`candidates`, `companies`, `users`, `teams`.\n\n"
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
    ]
