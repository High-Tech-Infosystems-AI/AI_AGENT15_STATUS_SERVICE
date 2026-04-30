"""Side-by-side comparison tools (compare_jobs / compare_candidates /
compare_companies / compare_periods) — parallel to compare_users and
compare_teams in data_tools.py.

Each tool reuses lower-level helpers (`pipeline_funnel`,
`candidate_detail`, `job_detail`, `company_detail`) and zips the
results into a table-friendly array so the model can produce a
side-by-side markdown table or a grouped chart in one pass.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai_chat_layer.access_middleware import CallerScope
from app.ai_chat_layer.mcp_client import McpClient, SchemaUnavailableError
from app.ai_chat_layer.mcp_server import semantic
from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")


# ─── compare_jobs ───────────────────────────────────────────────────

class CompareJobsArgs(BaseModel):
    job_ids: List[int] = Field(..., min_length=2, max_length=8)
    date_from: Optional[str] = None
    date_to: Optional[str] = None


def _compare_jobs(ctx: ToolContext, args: CompareJobsArgs) -> Dict[str, Any]:
    from app.ai_chat_layer.tools.data_tools import (
        FunnelArgs, _job_detail, _pipeline_funnel,
    )
    from app.ai_chat_layer.tools.data_tools import JobOnlyArgs

    out_jobs = []
    for jid in args.job_ids:
        if not ctx.scope.has_job(jid):
            continue
        head = _job_detail(ctx, JobOnlyArgs(job_id=jid))
        if head.get("not_found") or head.get("access_denied"):
            continue
        funnel = _pipeline_funnel(
            ctx,
            FunnelArgs(scope="job", scope_id=jid,
                       date_from=args.date_from, date_to=args.date_to,
                       limit_per_tag=3),
        )
        out_jobs.append({
            "job": {
                "id": head.get("id"),
                "title": head.get("title"),
                "status": head.get("status"),
                "openings": head.get("openings"),
                "applicant_count": head.get("applicant_count"),
                "recruiter_count": head.get("recruiter_count"),
                "company_name": (head.get("company") or {}).get("name"),
                "deadline": head.get("deadline"),
            },
            "by_tag": funnel.get("by_tag", []),
            "by_stage": funnel.get("by_stage", []),
            "by_type": funnel.get("by_type", {}),
            "total_candidates": funnel.get("total_candidates", 0),
        })
    return {"jobs": out_jobs, "count": len(out_jobs)}


# ─── compare_companies ──────────────────────────────────────────────

class CompareCompaniesArgs(BaseModel):
    company_ids: List[int] = Field(..., min_length=2, max_length=8)
    date_from: Optional[str] = None
    date_to: Optional[str] = None


def _compare_companies(
    ctx: ToolContext, args: CompareCompaniesArgs
) -> Dict[str, Any]:
    from app.ai_chat_layer.tools.data_tools import (
        CompanyOnlyArgs, FunnelArgs, _company_detail, _pipeline_funnel,
    )

    out_companies = []
    for cid in args.company_ids:
        if not ctx.scope.has_company(cid):
            continue
        head = _company_detail(ctx, CompanyOnlyArgs(company_id=cid))
        if head.get("not_found") or head.get("access_denied"):
            continue
        funnel = _pipeline_funnel(
            ctx,
            FunnelArgs(scope="company", scope_id=cid,
                       date_from=args.date_from, date_to=args.date_to,
                       limit_per_tag=3),
        )
        out_companies.append({
            "company": {
                "id": head.get("id"),
                "name": head.get("company_name") or head.get("name"),
                "jobs": head.get("jobs"),
                "active_jobs": head.get("active_jobs"),
                "applicants": head.get("applicants"),
            },
            "by_tag": funnel.get("by_tag", []),
            "by_stage": funnel.get("by_stage", []),
            "by_type": funnel.get("by_type", {}),
            "total_candidates": funnel.get("total_candidates", 0),
        })
    return {"companies": out_companies, "count": len(out_companies)}


# ─── compare_candidates ─────────────────────────────────────────────

class CompareCandidatesArgs(BaseModel):
    candidate_ids: List[str] = Field(..., min_length=2, max_length=6)


def _compare_candidates(
    ctx: ToolContext, args: CompareCandidatesArgs
) -> Dict[str, Any]:
    """Side-by-side profile + pipeline + match for 2-6 candidates.

    Reuses `candidate_detail` per candidate but slims the payload to the
    comparison-relevant fields so the model gets a tight diff-friendly
    structure.
    """
    from app.ai_chat_layer.tools.data_tools import (
        CandidateOnlyArgs, _candidate_detail,
    )

    profiles = []
    for cid in args.candidate_ids:
        cand_id = str(cid)
        if not ctx.scope.has_candidate(cand_id):
            profiles.append({
                "candidate_id": cand_id,
                "access_denied": True,
            })
            continue
        full = _candidate_detail(ctx, CandidateOnlyArgs(candidate_id=cand_id))
        if full.get("not_found"):
            profiles.append({"candidate_id": cand_id, "not_found": True})
            continue
        # Slim to comparison-relevant fields.
        profiles.append({
            "candidate_id": full.get("id"),
            "name": full.get("name"),
            "email": full.get("email"),
            "experience_years": full.get("experience_years"),
            "current_company": full.get("current_company"),
            "current_location": full.get("current_location"),
            "preferred_location": full.get("preferred_location"),
            "current_salary": full.get("current_salary"),
            "expected_salary": full.get("expected_salary"),
            "employment_status": full.get("employment_status"),
            "on_notice": full.get("on_notice"),
            "available_from": full.get("available_from"),
            "profile_source": full.get("profile_source"),
            "skills": full.get("skills"),
            "latest_status": full.get("latest_status"),
            "latest_match_score": full.get("latest_match_score"),
            "resume_versions_count": full.get("resume_versions_count"),
            "job_count": full.get("job_count"),
            "pipeline": [
                {
                    "job_title": p.get("job_title"),
                    "company_name": p.get("company_name"),
                    "stage": p.get("stage"),
                    "outcome": p.get("outcome"),
                    "applied_at": p.get("applied_at"),
                }
                for p in (full.get("pipeline") or [])
            ],
            "resume_summary": (
                {
                    "version": (full.get("resume") or {}).get("version"),
                    "summary": (full.get("resume") or {}).get("summary"),
                    "match": (full.get("resume") or {}).get("match"),
                }
                if full.get("resume") else None
            ),
        })
    return {"candidates": profiles, "count": len(profiles)}


# ─── compare_periods ────────────────────────────────────────────────

class ComparePeriodsArgs(BaseModel):
    measure: str = Field(
        ...,
        description="Same measure name accepted by query_data.",
    )
    period_a_from: str = Field(..., description="ISO date YYYY-MM-DD.")
    period_a_to: str = Field(..., description="ISO date YYYY-MM-DD.")
    period_b_from: str = Field(..., description="ISO date YYYY-MM-DD.")
    period_b_to: str = Field(..., description="ISO date YYYY-MM-DD.")
    dimensions: List[str] = Field(
        default_factory=list,
        description="Optional grouping dims (same names as query_data).",
    )
    filters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional filters (same as query_data).",
    )
    period_a_label: Optional[str] = Field(default=None, max_length=80)
    period_b_label: Optional[str] = Field(default=None, max_length=80)
    limit: int = Field(default=50, ge=1, le=200)
    order_by: Optional[str] = Field(default=None)
    order_dir: str = Field(default="desc", pattern="^(asc|desc)$")


def _run_period(
    ctx: ToolContext, *, measure: str, dimensions: List[str],
    filters: Dict[str, Any], date_from: str, date_to: str,
    limit: int, order_by: Optional[str], order_dir: str,
) -> List[Dict[str, Any]]:
    spec = semantic.QuerySpec(
        measure=measure,
        dimensions=list(dimensions or []),
        filters=dict(filters or {}),
        date_from=date_from, date_to=date_to,
        limit=limit, order_by=order_by, order_dir=order_dir,
    )
    sql, params, expanding = semantic.build_sql(spec, ctx.scope)
    try:
        return ctx.mcp.query(sql, params, expanding_keys=expanding or None)
    except SchemaUnavailableError as exc:
        return []


def _compare_periods(
    ctx: ToolContext, args: ComparePeriodsArgs
) -> Dict[str, Any]:
    """Run query_data twice and zip the rows with deltas per dimension."""
    try:
        rows_a = _run_period(
            ctx, measure=args.measure, dimensions=args.dimensions,
            filters=args.filters,
            date_from=args.period_a_from, date_to=args.period_a_to,
            limit=args.limit, order_by=args.order_by, order_dir=args.order_dir,
        )
        rows_b = _run_period(
            ctx, measure=args.measure, dimensions=args.dimensions,
            filters=args.filters,
            date_from=args.period_b_from, date_to=args.period_b_to,
            limit=args.limit, order_by=args.order_by, order_dir=args.order_dir,
        )
    except semantic.SemanticError as exc:
        return {"error": str(exc),
                "hint": "Call list_measures_dimensions to see what's available."}

    label_a = args.period_a_label or f"{args.period_a_from}..{args.period_a_to}"
    label_b = args.period_b_label or f"{args.period_b_from}..{args.period_b_to}"

    def _key(row: Dict[str, Any]) -> tuple:
        return tuple(row.get(d) for d in args.dimensions)

    def _val(row: Dict[str, Any]) -> Optional[float]:
        v = row.get(args.measure)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    map_a = {_key(r): _val(r) for r in rows_a}
    map_b = {_key(r): _val(r) for r in rows_b}

    if not args.dimensions:
        # Scalar compare.
        a = next(iter(map_a.values()), None)
        b = next(iter(map_b.values()), None)
        delta = (a - b) if (a is not None and b is not None) else None
        pct = None
        if delta is not None and b not in (None, 0):
            pct = round((a - b) / b * 100, 1)
        return {
            "measure": args.measure,
            "period_a": {"label": label_a, "value": a,
                          "from": args.period_a_from, "to": args.period_a_to},
            "period_b": {"label": label_b, "value": b,
                          "from": args.period_b_from, "to": args.period_b_to},
            "delta": delta,
            "delta_pct": pct,
        }

    # Dimensional compare.
    keys = list(map_a.keys())
    seen = set(keys)
    for k in map_b.keys():
        if k not in seen:
            keys.append(k)
            seen.add(k)
    out_rows = []
    for k in keys:
        a = map_a.get(k)
        b = map_b.get(k)
        delta = (a - b) if (a is not None and b is not None) else None
        pct = None
        if delta is not None and b not in (None, 0):
            pct = round((a - b) / b * 100, 1)
        row = {d: k[i] for i, d in enumerate(args.dimensions)}
        row.update({
            "period_a": a,
            "period_b": b,
            "delta": delta,
            "delta_pct": pct,
        })
        out_rows.append(row)

    return {
        "measure": args.measure,
        "dimensions": list(args.dimensions),
        "period_a": {"label": label_a,
                      "from": args.period_a_from, "to": args.period_a_to},
        "period_b": {"label": label_b,
                      "from": args.period_b_from, "to": args.period_b_to},
        "rows": out_rows,
        "count": len(out_rows),
    }


# ─── Tool builder ───────────────────────────────────────────────────

def build_tools(ctx: ToolContext) -> List[Any]:
    try:
        from langchain.tools import StructuredTool  # type: ignore
    except ImportError:
        try:
            from langchain_core.tools import StructuredTool  # type: ignore
        except ImportError:
            return []

    def _wrap(name, args_schema, fn, description):
        def _runner(**kwargs):
            args = args_schema(**kwargs) if kwargs else args_schema()
            start = time.monotonic()
            try:
                out = fn(ctx, args)
                ctx.add_trace(name, kwargs,
                              int((time.monotonic() - start) * 1000), True)
                return out
            except Exception as exc:
                ctx.add_trace(name, kwargs,
                              int((time.monotonic() - start) * 1000),
                              False, str(exc))
                logger.exception("tool %s failed", name)
                return {"error": str(exc)}
        return StructuredTool.from_function(
            func=_runner, name=name, description=description,
            args_schema=args_schema,
        )

    return [
        _wrap("compare_jobs", CompareJobsArgs, _compare_jobs,
              ("Side-by-side header + funnel for 2-8 jobs by id. Each "
               "entry has the job header (title, status, openings, "
               "applicant_count, recruiter_count, company_name, "
               "deadline) and its tag-bucketed funnel within the date "
               "range. Use for 'compare jobs A and B' / 'which job is "
               "moving faster'.")),
        _wrap("compare_companies", CompareCompaniesArgs, _compare_companies,
              ("Side-by-side header + funnel for 2-8 companies. Each "
               "entry has jobs / active_jobs / applicants counts plus "
               "the funnel by_tag / by_stage / total_candidates. Use "
               "for 'compare Acme vs Globex'.")),
        _wrap("compare_candidates", CompareCandidatesArgs, _compare_candidates,
              ("Side-by-side profile + pipeline + match score for 2-6 "
               "candidates by candidate_id. Each entry has the slim "
               "profile (experience, location, salary, availability, "
               "skills, latest_status, latest_match_score) plus their "
               "current pipeline rows across every job they're on. "
               "Use for 'compare these two candidates' / 'who's a "
               "better fit'.")),
        _wrap("compare_periods", ComparePeriodsArgs, _compare_periods,
              ("One-shot 'this period vs that period' delta. Pass any "
               "`measure` from the query_data catalog plus two date "
               "ranges (period_a / period_b) and optional `dimensions` + "
               "`filters`. Returns a row per dim with period_a, "
               "period_b, delta, and delta_pct (or a scalar if no "
               "dimensions). Use for 'this month vs last month for "
               "Acme', 'Q1 vs Q2 hires by recruiter', etc.")),
    ]
