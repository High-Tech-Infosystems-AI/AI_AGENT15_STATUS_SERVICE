"""Semantic-layer tools the model can call.

Three tools live here, each replacing roughly half the old
`data_tools.py` surface area:

  * `query_data(measure, dimensions[], filters{}, date_range, limit, order_by)` —
    the workhorse. Builds SQL declaratively from the catalog in
    `mcp_server/semantic.py`, applies the caller's scope automatically,
    caches the result for 5 min, and returns rows.

  * `describe_schema()` — surfaces the table/column catalog so the
    model can answer "what data is in this workspace" without the
    system prompt having to enumerate it.

  * `list_measures_dimensions()` — returns the catalog of measures /
    dimensions / filters so the model can self-discover what's askable
    without us writing a new tool every time.

Action tools (chart, PDF, simulation, suggestion buttons, elicitation)
live in their own files unchanged — those aren't queries.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai_chat_layer.mcp_client import SchemaUnavailableError
from app.ai_chat_layer.mcp_server import result_cache, semantic
from app.ai_chat_layer.mcp_server.elicitation import (
    ElicitationRequired, make_elicitation,
)
from app.ai_chat_layer.mcp_server.schema_meta import TABLES
from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")


# ─── query_data ───────────────────────────────────────────────────────

class QueryDataArgs(BaseModel):
    measure: str = Field(
        ...,
        description=(
            "The aggregate to compute. Application-grain (anchored on "
            "candidate_jobs): applications_count, candidates_count, "
            "joined_count, rejected_count, dropped_count, "
            "jobs_with_applicants_count, candidates_without_resume_count, "
            "multi_application_candidates_count, avg_time_to_hire_days, "
            "avg_match_score, avg_time_in_current_stage_days. Job-grain "
            "(anchored on job_openings — counts jobs with zero "
            "applicants too): jobs_total, active_jobs_count, "
            "openings_count, companies_count, "
            "jobs_without_applicants_count, jobs_without_recruiter_count, "
            "avg_applications_per_job. Pipeline-grain (anchored on "
            "pipelines — counts templates regardless of job presence): "
            "pipelines_count, orphan_pipelines_count. User-grain "
            "(anchored on users — admin-only; non-admins only see "
            "themselves): users_count, users_active_count, "
            "recruiters_without_jobs_count, users_without_team_count. "
            "Team-grain (anchored on teams — non-admins only see "
            "their teams): teams_count, teams_active_count, "
            "teams_without_members_count, teams_without_jobs_count. "
            "Call `list_measures_dimensions` to see the full catalog."
        ),
    )
    dimensions: List[str] = Field(
        default_factory=list,
        description=(
            "Zero or more grouping columns. Time: month / week / day / "
            "year / quarter. Entity: job / job_id / company / "
            "company_id / recruiter / recruiter_id / team / candidate / "
            "candidate_id / pipeline / pipeline_id. Job attributes: "
            "location / work_mode / deadline_bucket. Candidate "
            "attributes: candidate_location / experience_band / "
            "profile_source / current_company / employment_status. "
            "Pipeline state: stage / outcome_tag / terminal_status / "
            "job_status. User attributes (admin-only): role / "
            "user_status. Team attributes: department / team_status. "
            "Empty = a single scalar."
        ),
    )
    filters: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Filter dict, keyed by filter name. Single-value filters: "
            "{job_id: int}, {company_id: int}, {recruiter_id: int}, "
            "{team_id: int}, {candidate_id: str}, {pipeline_id: int}, "
            "{stage_name: str}, "
            "{outcome_tag: 'Selected'|'OfferAccepted'|...}, "
            "{terminal_status: 'joined'|'rejected'|'dropped'}, "
            "{job_status: 'ACTIVE'|'CLOSED'|...}, "
            "{location: str}, {work_mode: 'ONSITE'|'REMOTE'|'HYBRID'}, "
            "{deadline_before: 'YYYY-MM-DD'}, "
            "{deadline_after: 'YYYY-MM-DD'}, "
            "{candidate_location: str}, "
            "{experience_min: float}, {experience_max: float}, "
            "{current_salary_min: number}, {current_salary_max: number}, "
            "{expected_salary_min: number}, {expected_salary_max: number}, "
            "{on_notice: bool}, "
            "{available_before: 'YYYY-MM-DD'}, "
            "{available_after: 'YYYY-MM-DD'}, "
            "{profile_source: str}, {employment_status: str}, "
            "{current_company: str}, "
            "{skill_like: str} (substring on candidates.skills), "
            "{skill_name: str} (exact match against resume_skills), "
            "{match_score_min: number}, "
            "{stuck_days_min: int} (current stage set ≥ N days ago), "
            "{stage_changed_after: 'YYYY-MM-DD'}, "
            "{stage_changed_before: 'YYYY-MM-DD'}, "
            "{role_name: str} (admin-only), {user_enabled: bool}, "
            "{department: str}, {team_status: 'active'|'inactive'}, "
            "{team_active: bool}. Multi-value (list — IN): "
            "{job_ids: [...]}, {company_ids: [...]}, "
            "{recruiter_ids: [...]}, {team_ids: [...]}, "
            "{pipeline_ids: [...]}, {locations: [...]}, "
            "{work_modes: [...]}, {candidate_locations: [...]}, "
            "{profile_sources: [...]}, {role_names: [...]}, "
            "{departments: [...]}. "
            "Combine with a matching dimension (e.g. company_ids + "
            "dimensions=['company']) to get one row per entity in a "
            "single query."
        ),
    )
    date_from: Optional[str] = Field(
        default=None, description="ISO date YYYY-MM-DD (applied_at >=)."
    )
    date_to: Optional[str] = Field(
        default=None, description="ISO date YYYY-MM-DD (applied_at <=)."
    )
    limit: int = Field(default=50, ge=1, le=500)
    order_by: Optional[str] = Field(
        default=None,
        description="'measure' (default) or one of the dimension names.",
    )
    order_dir: str = Field(default="desc", pattern="^(asc|desc)$")


def _query_data(ctx: ToolContext, args: QueryDataArgs) -> Dict[str, Any]:
    spec = semantic.QuerySpec(
        measure=args.measure,
        dimensions=list(args.dimensions or []),
        filters=dict(args.filters or {}),
        date_from=args.date_from,
        date_to=args.date_to,
        limit=args.limit,
        order_by=args.order_by,
        order_dir=args.order_dir,
    )

    try:
        sql, params, expanding = semantic.build_sql(spec, ctx.scope)
    except semantic.SemanticError as exc:
        return {
            "error": str(exc),
            "hint": (
                "Call `list_measures_dimensions` to see what's available."
            ),
        }

    cache_key = result_cache.cache_key(sql, params, ctx.scope)
    cached = result_cache.get(cache_key)
    if cached is not None:
        return {**cached, "from_cache": True}

    try:
        rows = ctx.mcp.query(sql, params, expanding_keys=expanding or None)
    except SchemaUnavailableError as exc:
        # Re-raise so the standard `_timed` wrapper turns it into a
        # `data_unavailable` payload — same as the legacy data tools.
        raise exc
    payload = {
        "spec": {
            "measure": spec.measure,
            "dimensions": list(spec.dimensions),
            "filters": dict(spec.filters),
            "date_from": spec.date_from,
            "date_to": spec.date_to,
            "limit": spec.limit,
            "order_by": spec.order_by,
            "order_dir": spec.order_dir,
        },
        "rows": rows,
        "count": len(rows),
        "from_cache": False,
    }
    result_cache.set(cache_key, payload)
    return payload


# ─── describe_schema ──────────────────────────────────────────────────

class DescribeSchemaArgs(BaseModel):
    table: Optional[str] = Field(
        default=None,
        description=(
            "Table name (e.g. 'candidate_jobs') to drill into. Omit to "
            "list every table with one-line descriptions."
        ),
    )


def _describe_schema(ctx: ToolContext, args: DescribeSchemaArgs) -> Dict[str, Any]:
    is_admin = ctx.scope.unscoped
    if args.table:
        if args.table not in TABLES:
            return {
                "error": f"Unknown table {args.table!r}",
                "available": sorted(TABLES.keys()),
            }
        t = TABLES[args.table]
        return {
            "name": t.name,
            "alias": t.alias,
            "description": t.description,
            "primary_key": t.primary_key,
            "columns": [
                {
                    "name": c.name,
                    "type": c.type_hint,
                    "description": c.description,
                }
                for c in t.columns
                # Hide PII columns from non-admins. The model can still
                # query the table; it just doesn't see PII column names
                # advertised here so it can't ask for them by accident.
                if (is_admin or not c.pii)
            ],
        }
    return {
        "tables": [
            {"name": t.name, "alias": t.alias, "description": t.description}
            for t in TABLES.values()
        ],
    }


# ─── list_measures_dimensions ─────────────────────────────────────────

class _NoArgs(BaseModel):
    pass


def _list_measures_dimensions(ctx: ToolContext, _args: _NoArgs) -> Dict[str, Any]:
    return {
        "measures": semantic.measures_catalog(),
        "dimensions": semantic.dimensions_catalog(),
        "filters": semantic.filters_catalog(),
    }


# ─── Tool builder ────────────────────────────────────────────────────

def build_tools(ctx: ToolContext) -> List[Any]:
    try:
        from langchain.tools import StructuredTool  # type: ignore
    except ImportError:
        try:
            from langchain_core.tools import StructuredTool  # type: ignore
        except ImportError:
            return []

    def _wrap(name: str, args_schema, fn, description: str):
        def _runner(**kwargs):
            args = args_schema(**kwargs) if kwargs else args_schema()
            start = time.monotonic()
            try:
                out = fn(ctx, args)
                ctx.add_trace(name, kwargs,
                              int((time.monotonic() - start) * 1000), True)
                return out
            except ElicitationRequired as exc:
                payload = make_elicitation(exc.spec, note=exc.note)
                # Surface like the data_tools wrapper does.
                ctx.add_output_ref({
                    "type": "ai_elicitation",
                    "id": payload["elicitation_required"].get("id"),
                    "params": payload["elicitation_required"],
                })
                ctx.add_trace(name, kwargs,
                              int((time.monotonic() - start) * 1000), True)
                return {"elicitation_pending": True, "note": payload.get("note")}
            except SchemaUnavailableError as exc:
                ctx.add_trace(
                    name, kwargs,
                    int((time.monotonic() - start) * 1000), False,
                    f"schema_unavailable: {exc.missing or 'unknown'}",
                )
                return {
                    "data_unavailable": True,
                    "missing": exc.missing,
                    "note": (
                        "The required column/table is not in this "
                        "workspace's schema. Continue with whatever data "
                        "you have; do not refuse the request."
                    ),
                }
            except Exception as exc:
                ctx.add_trace(name, kwargs,
                              int((time.monotonic() - start) * 1000),
                              False, str(exc))
                logger.exception("tool %s failed", name)
                return {"error": str(exc)}

        return StructuredTool.from_function(
            func=_runner, name=name,
            description=description, args_schema=args_schema,
        )

    return [
        _wrap(
            "query_data", QueryDataArgs, _query_data,
            (
                "Run a typed analytics query. Pick one MEASURE, zero "
                "or more DIMENSIONS, any FILTERS, optional date range, "
                "limit, order_by. Anchor (candidate_jobs vs "
                "job_openings) is auto-selected from the measure so "
                "job-level questions correctly include jobs with zero "
                "applicants. Examples:\n"
                "  • Active jobs at a company: "
                "measure=active_jobs_count, filters={company_id: 12}\n"
                "  • Total openings at a company: "
                "measure=openings_count, filters={company_id: 12}\n"
                "  • Funnel by tag for one job: "
                "measure=candidates_count, dimensions=[outcome_tag], "
                "filters={job_id: 171}\n"
                "  • Compare 2 companies side-by-side: "
                "measure=applications_count, dimensions=[company], "
                "filters={company_ids: [12, 47]}, "
                "date_from='2026-04-01'\n"
                "  • Compare recruiters: measure=candidates_count, "
                "dimensions=[recruiter, outcome_tag], "
                "filters={recruiter_ids: [42, 51]}\n"
                "  • Monthly applications for a company: "
                "measure=applications_count, dimensions=[month], "
                "filters={company_id: 12}\n"
                "  • Top recruiters this quarter: "
                "measure=candidates_count, dimensions=[recruiter], "
                "date_from='2026-04-01', date_to='2026-06-30', "
                "limit=10\n"
                "  • Hiring volume by pipeline: measure=joined_count, "
                "dimensions=[pipeline]\n"
                "  • Jobs with zero applicants: "
                "measure=jobs_without_applicants_count\n"
                "  • Jobs with no recruiter assigned: "
                "measure=jobs_without_recruiter_count\n"
                "  • Average applications per job at a company: "
                "measure=avg_applications_per_job, "
                "filters={company_id: 12}\n"
                "  • Headcount by location: measure=jobs_total, "
                "dimensions=[location]\n"
                "  • Remote vs onsite jobs: measure=jobs_total, "
                "dimensions=[work_mode]\n"
                "  • Jobs nearing deadline: measure=jobs_total, "
                "dimensions=[deadline_bucket]\n"
                "  • Candidates by city: measure=candidates_count, "
                "dimensions=[candidate_location], limit=20\n"
                "  • Candidates by experience band: "
                "measure=candidates_count, dimensions=[experience_band]\n"
                "  • Sourcing channel breakdown: "
                "measure=candidates_count, dimensions=[profile_source]\n"
                "  • Candidates with no resume in scope: "
                "measure=candidates_without_resume_count\n"
                "  • Candidates applied to multiple jobs: "
                "measure=multi_application_candidates_count\n"
                "  • Average days to hire by company: "
                "measure=avg_time_to_hire_days, dimensions=[company]\n"
                "  • Candidates with Python skill: "
                "measure=candidates_count, filters={skill_like: 'python'}\n"
                "  • Top candidates joinable in 30 days: "
                "measure=candidates_count, "
                "filters={available_before: '2026-05-30'}\n"
                "  • Salary band — current 1-2M INR: "
                "measure=candidates_count, "
                "filters={current_salary_min: 1000000, "
                "current_salary_max: 2000000}\n"
                "  • Total pipelines: measure=pipelines_count\n"
                "  • Pipelines with no jobs: "
                "measure=orphan_pipelines_count\n"
                "  • Avg days in current stage by pipeline: "
                "measure=avg_time_in_current_stage_days, "
                "dimensions=[pipeline]\n"
                "  • Stuck candidates (>14d in current stage): "
                "measure=candidates_count, "
                "filters={stuck_days_min: 14}\n"
                "  • Compare two pipelines: "
                "measure=joined_count, dimensions=[pipeline], "
                "filters={pipeline_ids: [3, 7]}\n"
                "  • Total active users: measure=users_active_count\n"
                "  • Recruiters with no jobs: "
                "measure=recruiters_without_jobs_count\n"
                "  • Users by role: measure=users_count, "
                "dimensions=[role]\n"
                "  • New users this month: measure=users_count, "
                "date_from='2026-04-01'\n"
                "  • Total active teams: measure=teams_active_count\n"
                "  • Teams with no jobs assigned: "
                "measure=teams_without_jobs_count\n"
                "  • Teams by department: measure=teams_count, "
                "dimensions=[department]\n"
                "  • Engineering teams only: measure=teams_count, "
                "filters={department: 'Engineering'}\n"
                "When unsure, call `list_measures_dimensions` first. "
                "ACL is enforced automatically — recruiters only see "
                "their assigned jobs' data, on every anchor."
            ),
        ),
        _wrap(
            "list_measures_dimensions", _NoArgs, _list_measures_dimensions,
            (
                "Return the catalog of measures (aggregates), "
                "dimensions (groupings), and filters available to "
                "`query_data`. Call this whenever you're unsure what "
                "the right knobs are — it replaces the old per-tool "
                "lookup."
            ),
        ),
        _wrap(
            "describe_schema", DescribeSchemaArgs, _describe_schema,
            (
                "Inspect the database schema we expose. With no `table` "
                "arg returns the catalog of tables with one-line "
                "descriptions. With `table='<name>'` returns the column "
                "list (PII columns hidden from non-admin callers)."
            ),
        ),
    ]
