"""Semantic-layer query builder.

The agent's `query_data` tool calls into this module with a declarative
spec — `(measure, dimensions, filters, time_range)` — and gets back a
list of rows + the SQL/params it ran. The model never composes SQL; it
only chooses from the registered measures / dimensions / filters here,
which keeps ACL, schema knowledge, and audit centralised.

Adding a new "ask" = adding an entry to MEASURES / DIMENSIONS / FILTERS,
not writing a new tool.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.ai_chat_layer.access_middleware import CallerScope
from app.ai_chat_layer.mcp_client import McpClient
from app.ai_chat_layer.mcp_server.schema_meta import (
    JOIN_GRAPH, TABLES, alias_for, join_path,
)

logger = logging.getLogger("app_logger")


# ─── Catalog primitives ───────────────────────────────────────────────

@dataclass(frozen=True)
class Measure:
    """An aggregate column. SQL goes into the SELECT.

    `requires_aliases` lists every alias the SQL fragment references —
    the query builder uses it to compute which JOINs are mandatory.

    `anchor` declares which table the query starts FROM. Most analytics
    are application-grain so the default `cj` (candidate_jobs) is right.
    Job-grain measures (e.g. `jobs_total`, `openings_count`) anchor on
    `j` so they correctly count jobs that have zero applicants too.
    """
    name: str
    description: str
    sql: str
    requires_aliases: Tuple[str, ...]
    anchor: str = "cj"


@dataclass(frozen=True)
class Dimension:
    """A grouping column. SQL goes into both SELECT and GROUP BY.

    `order_sql` (optional) lets a dimension declare a custom ORDER BY
    expression — e.g. stage by `ps.order` rather than alphabetic.
    """
    name: str
    description: str
    sql: str
    requires_aliases: Tuple[str, ...]
    order_sql: Optional[str] = None
    inner_join_aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Filter:
    """A WHERE-clause fragment with `:param_name` placeholders."""
    name: str
    description: str
    sql_template: str
    requires_aliases: Tuple[str, ...]
    params: Tuple[str, ...]      # names of bind params it uses


# ─── Catalog ──────────────────────────────────────────────────────────

# Default anchor for application-grain measures. Job-grain measures
# override via `Measure.anchor`. The `ANCHOR_DATE_COL` map tells the
# query builder which column the user's `date_from` / `date_to` should
# constrain when no explicit date column is requested — applications
# use applied_at, jobs use created_at.
ANCHOR_DATE_COL: Dict[str, str] = {
    "cj": "cj.applied_at",
    "j":  "j.created_at",
    "co": "co.id",   # companies have no created_at; effectively disable date filter
    "pl": "pl.created_at",
    "u":  "u.created_at",
    "t":  "t.created_at",
}

# Per-anchor scope predicate. Recruiters get filtered to their assigned
# jobs whichever table the query is anchored on; the predicate has to
# reference an alias guaranteed to be in scope from that anchor.
_SCOPE_PREDICATE: Dict[str, str] = {
    "cj": "cj.job_id IN :scope_jobs",
    "j":  "j.id IN :scope_jobs",
    "co": "co.id IN (SELECT company_id FROM job_openings WHERE id IN :scope_jobs)",
    "pl": "pl.id IN (SELECT pipeline_id FROM job_openings WHERE id IN :scope_jobs)",
    # User-anchored queries are admin-only effectively; non-admins see
    # only themselves. Implemented inside _scope_where to inject
    # CallerScope.user_id.
    "u":  "u.id = :scope_self_user_id",
    # Team-anchored queries: non-admins see only teams they belong to.
    "t":  ("t.id IN (SELECT team_id FROM team_members "
           "WHERE user_id = :scope_self_user_id)"),
}

MEASURES: Dict[str, Measure] = {
    "applications_count": Measure(
        name="applications_count",
        description="Number of applications (rows in candidate_jobs).",
        sql="COUNT(DISTINCT cj.id)",
        requires_aliases=("cj",),
    ),
    "candidates_count": Measure(
        name="candidates_count",
        description="Distinct candidates across the filtered scope.",
        sql="COUNT(DISTINCT cj.candidate_id)",
        requires_aliases=("cj",),
    ),
    "joined_count": Measure(
        name="joined_count",
        description="Candidates with a terminal `JOINED` status.",
        sql="COUNT(DISTINCT CASE WHEN LOWER(cjs.type) = 'joined' THEN cj.id END)",
        requires_aliases=("cj", "cjs"),
    ),
    "rejected_count": Measure(
        name="rejected_count",
        description="Candidates with a terminal `REJECTED` status.",
        sql="COUNT(DISTINCT CASE WHEN LOWER(cjs.type) = 'rejected' THEN cj.id END)",
        requires_aliases=("cj", "cjs"),
    ),
    "dropped_count": Measure(
        name="dropped_count",
        description="Candidates marked `DROPPED`.",
        sql="COUNT(DISTINCT CASE WHEN LOWER(cjs.type) = 'dropped' THEN cj.id END)",
        requires_aliases=("cj", "cjs"),
    ),
    "openings_count": Measure(
        name="openings_count",
        description=(
            "Sum of openings across matching jobs. Anchored on "
            "job_openings so jobs with zero applications still count."
        ),
        sql="SUM(j.openings)",
        requires_aliases=("j",),
        anchor="j",
    ),
    "jobs_with_applicants_count": Measure(
        name="jobs_with_applicants_count",
        description=(
            "Distinct jobs that have at least one application. "
            "Application-grain — anchored on candidate_jobs."
        ),
        sql="COUNT(DISTINCT cj.job_id)",
        requires_aliases=("cj",),
    ),
    # `jobs_count` retained as alias of jobs_with_applicants_count for
    # backwards compat; new code should use jobs_total.
    "jobs_count": Measure(
        name="jobs_count",
        description=(
            "DEPRECATED alias of jobs_with_applicants_count. Prefer "
            "`jobs_total` for 'all jobs in scope' (jobs with no "
            "applicants included)."
        ),
        sql="COUNT(DISTINCT cj.job_id)",
        requires_aliases=("cj",),
    ),
    "jobs_total": Measure(
        name="jobs_total",
        description=(
            "Total distinct jobs in scope (counted from job_openings "
            "directly — includes jobs with zero applicants)."
        ),
        sql="COUNT(DISTINCT j.id)",
        requires_aliases=("j",),
        anchor="j",
    ),
    "active_jobs_count": Measure(
        name="active_jobs_count",
        description=(
            "Distinct jobs whose status is ACTIVE in scope. Anchored on "
            "job_openings so brand-new jobs count."
        ),
        sql="COUNT(DISTINCT CASE WHEN UPPER(j.status) = 'ACTIVE' THEN j.id END)",
        requires_aliases=("j",),
        anchor="j",
    ),
    "companies_count": Measure(
        name="companies_count",
        description="Distinct companies in scope.",
        sql="COUNT(DISTINCT j.company_id)",
        requires_aliases=("j",),
        anchor="j",
    ),
    "jobs_without_applicants_count": Measure(
        name="jobs_without_applicants_count",
        description=(
            "Distinct jobs in scope that have ZERO applications "
            "(no row in candidate_jobs). Anchored on job_openings."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN NOT EXISTS("
            "SELECT 1 FROM candidate_jobs cj2 WHERE cj2.job_id = j.id"
            ") THEN j.id END)"
        ),
        requires_aliases=("j",),
        anchor="j",
    ),
    "jobs_without_recruiter_count": Measure(
        name="jobs_without_recruiter_count",
        description=(
            "Distinct jobs in scope with NO recruiter assigned "
            "(no row in user_jobs_assigned). Anchored on job_openings."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN NOT EXISTS("
            "SELECT 1 FROM user_jobs_assigned uja2 WHERE uja2.job_id = j.id"
            ") THEN j.id END)"
        ),
        requires_aliases=("j",),
        anchor="j",
    ),
    "avg_applications_per_job": Measure(
        name="avg_applications_per_job",
        description=(
            "Average number of applications per job in scope. Counts "
            "every job (including zero-application jobs) in the "
            "denominator."
        ),
        sql=(
            "ROUND(COUNT(DISTINCT cj.id) / "
            "NULLIF(COUNT(DISTINCT j.id), 0), 2)"
        ),
        requires_aliases=("j", "cj"),
        anchor="j",
    ),
    "candidates_without_resume_count": Measure(
        name="candidates_without_resume_count",
        description=(
            "Distinct candidates with no row in resume_personal_details. "
            "Application-grain (anchored on candidate_jobs)."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN NOT EXISTS("
            "SELECT 1 FROM resume_personal_details rp2 "
            "WHERE rp2.candidate_id = cj.candidate_id) "
            "THEN cj.candidate_id END)"
        ),
        requires_aliases=("cj",),
    ),
    "multi_application_candidates_count": Measure(
        name="multi_application_candidates_count",
        description=(
            "Candidates who applied to MORE THAN ONE job in scope "
            "(distinct candidate ids whose cj rows span 2+ jobs)."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN EXISTS("
            "SELECT 1 FROM candidate_jobs cj3 "
            "WHERE cj3.candidate_id = cj.candidate_id "
            "AND cj3.job_id <> cj.job_id) "
            "THEN cj.candidate_id END)"
        ),
        requires_aliases=("cj",),
    ),
    "avg_time_to_hire_days": Measure(
        name="avg_time_to_hire_days",
        description=(
            "Average days between applied_at and joined_at for "
            "applications that ended with terminal status 'joined'."
        ),
        sql=(
            "ROUND(AVG(CASE WHEN LOWER(cjs.type) = 'joined' "
            "AND cjs.joined_at IS NOT NULL "
            "THEN DATEDIFF(cjs.joined_at, cj.applied_at) END), 1)"
        ),
        requires_aliases=("cj", "cjs"),
    ),
    "avg_match_score": Measure(
        name="avg_match_score",
        description=(
            "Average overall_match_score across resume_matching rows in "
            "scope. Includes every version; for per-candidate latest "
            "score use the `candidate_resume_detail` tool."
        ),
        sql="AVG(rm.overall_match_score)",
        requires_aliases=("cj", "rm"),
    ),
    "pipelines_count": Measure(
        name="pipelines_count",
        description=(
            "Distinct pipelines (templates) in scope, excluding "
            "soft-deleted ones. Anchored on pipelines."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN pl.deleted_at IS NULL "
            "THEN pl.id END)"
        ),
        requires_aliases=("pl",),
        anchor="pl",
    ),
    "orphan_pipelines_count": Measure(
        name="orphan_pipelines_count",
        description=(
            "Pipelines (templates) with NO jobs using them. Anchored on "
            "pipelines."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN pl.deleted_at IS NULL AND NOT EXISTS("
            "SELECT 1 FROM job_openings j2 WHERE j2.pipeline_id = pl.id"
            ") THEN pl.id END)"
        ),
        requires_aliases=("pl",),
        anchor="pl",
    ),
    "avg_time_in_current_stage_days": Measure(
        name="avg_time_in_current_stage_days",
        description=(
            "Average days candidates have been sitting in their CURRENT "
            "pipeline stage (DATEDIFF from cps.created_at to today, "
            "where cps.latest=1). Captures stuck-pipeline severity."
        ),
        sql="ROUND(AVG(DATEDIFF(NOW(), cps.created_at)), 1)",
        requires_aliases=("cj", "cps"),
    ),
    "users_count": Measure(
        name="users_count",
        description=(
            "Distinct live users (deleted_at IS NULL). Anchored on users; "
            "admin-only — non-admins see just themselves."
        ),
        sql="COUNT(DISTINCT CASE WHEN u.deleted_at IS NULL THEN u.id END)",
        requires_aliases=("u",),
        anchor="u",
    ),
    "users_active_count": Measure(
        name="users_active_count",
        description=(
            "Distinct active users (deleted_at IS NULL AND enable = 1)."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN u.deleted_at IS NULL "
            "AND u.enable = 1 THEN u.id END)"
        ),
        requires_aliases=("u",),
        anchor="u",
    ),
    "recruiters_without_jobs_count": Measure(
        name="recruiters_without_jobs_count",
        description=(
            "Live users with role 'user' (recruiters) who have NO row "
            "in user_jobs_assigned. Joins roles automatically."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN u.deleted_at IS NULL "
            "AND LOWER(r.name) = 'user' AND NOT EXISTS("
            "SELECT 1 FROM user_jobs_assigned uja2 "
            "WHERE uja2.user_id = u.id) THEN u.id END)"
        ),
        requires_aliases=("u", "r"),
        anchor="u",
    ),
    "users_without_team_count": Measure(
        name="users_without_team_count",
        description=(
            "Live users who are NOT in any team (no row in team_members)."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN u.deleted_at IS NULL AND NOT EXISTS("
            "SELECT 1 FROM team_members tm2 "
            "WHERE tm2.user_id = u.id) THEN u.id END)"
        ),
        requires_aliases=("u",),
        anchor="u",
    ),
    "teams_count": Measure(
        name="teams_count",
        description=(
            "Distinct live teams (deleted_at IS NULL). Anchored on teams."
        ),
        sql="COUNT(DISTINCT CASE WHEN t.deleted_at IS NULL THEN t.id END)",
        requires_aliases=("t",),
        anchor="t",
    ),
    "teams_active_count": Measure(
        name="teams_active_count",
        description=(
            "Distinct teams where status = 'active' AND deleted_at IS NULL."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN t.deleted_at IS NULL "
            "AND LOWER(t.status) = 'active' THEN t.id END)"
        ),
        requires_aliases=("t",),
        anchor="t",
    ),
    "teams_without_members_count": Measure(
        name="teams_without_members_count",
        description=(
            "Live teams with NO members (no row in team_members)."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN t.deleted_at IS NULL AND NOT EXISTS("
            "SELECT 1 FROM team_members tm2 "
            "WHERE tm2.team_id = t.id) THEN t.id END)"
        ),
        requires_aliases=("t",),
        anchor="t",
    ),
    "teams_without_jobs_count": Measure(
        name="teams_without_jobs_count",
        description=(
            "Live teams with NO direct job assignment (no row in "
            "job_team_assignments)."
        ),
        sql=(
            "COUNT(DISTINCT CASE WHEN t.deleted_at IS NULL AND NOT EXISTS("
            "SELECT 1 FROM job_team_assignments jta2 "
            "WHERE jta2.team_id = t.id) THEN t.id END)"
        ),
        requires_aliases=("t",),
        anchor="t",
    ),
}

DIMENSIONS: Dict[str, Dimension] = {
    "stage": Dimension(
        name="stage",
        description="Pipeline stage label (from pipeline_stages.name).",
        sql="ps.name",
        requires_aliases=("cj", "cps", "ps"),
        order_sql="MIN(ps.`order`), MIN(ps.id)",
        inner_join_aliases=("cps", "ps"),
    ),
    "outcome_tag": Dimension(
        name="outcome_tag",
        description=(
            "Cross-stage outcome category from pipeline_stage_status.tag "
            "(Sourcing / Screening / LineUps / TurnUps / Selected / "
            "OfferReleased / OfferAccepted)."
        ),
        sql="pss.tag",
        requires_aliases=("cj", "cps", "pss"),
        inner_join_aliases=("cps", "pss"),
    ),
    "terminal_status": Dimension(
        name="terminal_status",
        description="JOINED / REJECTED / DROPPED from candidate_job_status.type.",
        sql="LOWER(cjs.type)",
        requires_aliases=("cj", "cjs"),
        inner_join_aliases=("cjs",),
    ),
    "job": Dimension(
        name="job",
        description="Job title.",
        sql="j.title",
        requires_aliases=("cj", "j"),
    ),
    "job_id": Dimension(
        name="job_id",
        description="Job integer id.",
        sql="cj.job_id",
        requires_aliases=("cj",),
    ),
    "company": Dimension(
        name="company",
        description="Company name.",
        sql="co.company_name",
        requires_aliases=("cj", "j", "co"),
    ),
    "company_id": Dimension(
        name="company_id",
        description="Company id.",
        sql="j.company_id",
        requires_aliases=("cj", "j"),
    ),
    "recruiter": Dimension(
        name="recruiter",
        description="Recruiter name (joined via user_jobs_assigned).",
        sql="u.name",
        requires_aliases=("cj", "uja", "u"),
        inner_join_aliases=("uja", "u"),
    ),
    "recruiter_id": Dimension(
        name="recruiter_id",
        description="Recruiter user id.",
        sql="u.id",
        requires_aliases=("cj", "uja", "u"),
        inner_join_aliases=("uja", "u"),
    ),
    "team": Dimension(
        name="team",
        description="Team name (via team_members → users → user_jobs_assigned).",
        sql="t.name",
        requires_aliases=("cj", "uja", "u", "tm", "t"),
        inner_join_aliases=("uja", "u", "tm", "t"),
    ),
    "month": Dimension(
        name="month",
        description="Application month bucket (YYYY-MM).",
        sql="DATE_FORMAT(cj.applied_at, '%Y-%m')",
        requires_aliases=("cj",),
        order_sql="MIN(cj.applied_at)",
    ),
    "week": Dimension(
        name="week",
        description="Application ISO week bucket (YYYY-WW).",
        sql="DATE_FORMAT(cj.applied_at, '%x-W%v')",
        requires_aliases=("cj",),
        order_sql="MIN(cj.applied_at)",
    ),
    "day": Dimension(
        name="day",
        description="Application date (YYYY-MM-DD).",
        sql="DATE(cj.applied_at)",
        requires_aliases=("cj",),
    ),
    "candidate": Dimension(
        name="candidate",
        description="Candidate display name.",
        sql="c.candidate_name",
        requires_aliases=("cj", "c"),
    ),
    "candidate_id": Dimension(
        name="candidate_id",
        description="Candidate string id.",
        sql="cj.candidate_id",
        requires_aliases=("cj",),
    ),
    "job_status": Dimension(
        name="job_status",
        description="Job lifecycle (job_openings.status).",
        sql="UPPER(j.status)",
        requires_aliases=("cj", "j"),
    ),
    "pipeline": Dimension(
        name="pipeline",
        description=(
            "Pipeline template name (pipelines.name). Reachable from "
            "either j-anchored or cj-anchored queries."
        ),
        sql="pl.name",
        requires_aliases=("j", "pl"),
    ),
    "pipeline_id": Dimension(
        name="pipeline_id",
        description="Pipeline template id.",
        sql="j.pipeline_id",
        requires_aliases=("j",),
    ),
    "year": Dimension(
        name="year",
        description="Application year bucket (YYYY).",
        sql="DATE_FORMAT(cj.applied_at, '%Y')",
        requires_aliases=("cj",),
        order_sql="MIN(cj.applied_at)",
    ),
    "quarter": Dimension(
        name="quarter",
        description="Application calendar quarter bucket (YYYY-Q#).",
        sql="CONCAT(YEAR(cj.applied_at), '-Q', QUARTER(cj.applied_at))",
        requires_aliases=("cj",),
        order_sql="MIN(cj.applied_at)",
    ),
    "location": Dimension(
        name="location",
        description="Job location (job_openings.location).",
        sql="j.location",
        requires_aliases=("j",),
    ),
    "work_mode": Dimension(
        name="work_mode",
        description="Job work mode — ONSITE / REMOTE / HYBRID.",
        sql="UPPER(j.work_mode)",
        requires_aliases=("j",),
    ),
    "deadline_bucket": Dimension(
        name="deadline_bucket",
        description=(
            "Bucket jobs by their deadline relative to today: "
            "'overdue' / 'next-7d' / 'next-30d' / 'later' / 'no-deadline'."
        ),
        sql=(
            "CASE "
            "WHEN j.deadline IS NULL THEN 'no-deadline' "
            "WHEN j.deadline < CURDATE() THEN 'overdue' "
            "WHEN j.deadline < DATE_ADD(CURDATE(), INTERVAL 7 DAY) THEN 'next-7d' "
            "WHEN j.deadline < DATE_ADD(CURDATE(), INTERVAL 30 DAY) THEN 'next-30d' "
            "ELSE 'later' END"
        ),
        requires_aliases=("j",),
    ),
    "candidate_location": Dimension(
        name="candidate_location",
        description="Candidate's current city / region (candidates.current_location).",
        sql="c.current_location",
        requires_aliases=("cj", "c"),
    ),
    "experience_band": Dimension(
        name="experience_band",
        description=(
            "Bucket candidates by years of experience: '0-2' / '2-5' / "
            "'5-10' / '10+' / 'unknown'."
        ),
        sql=(
            "CASE "
            "WHEN c.experience IS NULL THEN 'unknown' "
            "WHEN c.experience < 2 THEN '0-2' "
            "WHEN c.experience < 5 THEN '2-5' "
            "WHEN c.experience < 10 THEN '5-10' "
            "ELSE '10+' END"
        ),
        requires_aliases=("cj", "c"),
    ),
    "profile_source": Dimension(
        name="profile_source",
        description="Sourcing channel — LinkedIn / Naukri / Referral / etc.",
        sql="c.profile_source",
        requires_aliases=("cj", "c"),
    ),
    "current_company": Dimension(
        name="current_company",
        description="Candidate's current employer (candidates.current_company).",
        sql="c.current_company",
        requires_aliases=("cj", "c"),
    ),
    "employment_status": Dimension(
        name="employment_status",
        description="Employment status — Active / On Notice / etc.",
        sql="c.employment_status",
        requires_aliases=("cj", "c"),
    ),
    "role": Dimension(
        name="role",
        description=(
            "User role name (super_admin / admin / user / …). 'unassigned' "
            "if the user has no role_id."
        ),
        sql="COALESCE(r.name, 'unassigned')",
        requires_aliases=("u", "r"),
    ),
    "user_status": Dimension(
        name="user_status",
        description=(
            "Bucket users by lifecycle: 'active' / 'disabled' / 'deleted'."
        ),
        sql=(
            "CASE WHEN u.deleted_at IS NOT NULL THEN 'deleted' "
            "WHEN u.enable = 1 THEN 'active' ELSE 'disabled' END"
        ),
        requires_aliases=("u",),
    ),
    "department": Dimension(
        name="department",
        description="Owning department of a team (teams.department).",
        sql="COALESCE(t.department, 'unassigned')",
        requires_aliases=("t",),
    ),
    "team_status": Dimension(
        name="team_status",
        description=(
            "Bucket teams by lifecycle: 'active' / 'inactive' / 'deleted'."
        ),
        sql=(
            "CASE WHEN t.deleted_at IS NOT NULL THEN 'deleted' "
            "WHEN LOWER(t.status) = 'active' THEN 'active' "
            "ELSE 'inactive' END"
        ),
        requires_aliases=("t",),
    ),
}

FILTERS: Dict[str, Filter] = {
    # Job-grain filters use the `j` alias so they work for both cj- and
    # j-anchored queries (j is reachable in both). The `cj.job_id` and
    # `j.id` columns are equal by construction.
    "job_id": Filter("job_id", "Restrict to one job.",
                     "j.id = :job_id", ("j",), ("job_id",)),
    "job_ids": Filter(
        "job_ids", "Restrict to a list of jobs (comparisons / batches).",
        "j.id IN :job_ids", ("j",), ("job_ids",),
    ),
    "company_id": Filter("company_id", "Restrict to one company.",
                         "j.company_id = :company_id",
                         ("j",), ("company_id",)),
    "company_ids": Filter(
        "company_ids", "Restrict to a list of companies.",
        "j.company_id IN :company_ids", ("j",), ("company_ids",),
    ),
    "recruiter_id": Filter(
        "recruiter_id",
        "Restrict to candidates on a user's assigned jobs.",
        "uja.user_id = :recruiter_id",
        ("uja",), ("recruiter_id",),
    ),
    "recruiter_ids": Filter(
        "recruiter_ids",
        "Restrict to candidates on multiple users' assigned jobs.",
        "uja.user_id IN :recruiter_ids",
        ("uja",), ("recruiter_ids",),
    ),
    "team_id": Filter(
        "team_id", "Restrict to jobs assigned to a team's members.",
        "uja.user_id IN (SELECT user_id FROM team_members WHERE team_id = :team_id)",
        ("uja",), ("team_id",),
    ),
    "team_ids": Filter(
        "team_ids", "Restrict to jobs assigned to multiple teams' members.",
        "uja.user_id IN (SELECT user_id FROM team_members WHERE team_id IN :team_ids)",
        ("uja",), ("team_ids",),
    ),
    "candidate_id": Filter(
        "candidate_id", "Restrict to one candidate.",
        "cj.candidate_id = :candidate_id", ("cj",), ("candidate_id",),
    ),
    "stage_name": Filter(
        "stage_name", "Stage label match (case-insensitive).",
        "UPPER(ps.name) = UPPER(:stage_name)",
        ("cj", "cps", "ps"), ("stage_name",),
    ),
    "outcome_tag": Filter(
        "outcome_tag",
        "Outcome tag match — Sourcing / Screening / LineUps / TurnUps / "
        "Selected / OfferReleased / OfferAccepted.",
        "pss.tag = :outcome_tag",
        ("cj", "cps", "pss"), ("outcome_tag",),
    ),
    "terminal_status": Filter(
        "terminal_status",
        "JOINED / REJECTED / DROPPED match.",
        "LOWER(cjs.type) = LOWER(:terminal_status)",
        ("cj", "cjs"), ("terminal_status",),
    ),
    "job_status": Filter(
        "job_status",
        "Job lifecycle filter (ACTIVE / CLOSED / ON_HOLD / ...).",
        "UPPER(j.status) = UPPER(:job_status)",
        ("j",), ("job_status",),
    ),
    "pipeline_id": Filter(
        "pipeline_id", "Restrict to one pipeline template.",
        "j.pipeline_id = :pipeline_id",
        ("j",), ("pipeline_id",),
    ),
    "pipeline_ids": Filter(
        "pipeline_ids",
        "Restrict to a list of pipeline templates (compare side-by-side).",
        "j.pipeline_id IN :pipeline_ids",
        ("j",), ("pipeline_ids",),
    ),
    "stuck_days_min": Filter(
        "stuck_days_min",
        (
            "Only candidates whose current pipeline stage was set "
            "(cps.latest=1) at least N days ago — i.e. stuck."
        ),
        "DATEDIFF(NOW(), cps.created_at) >= :stuck_days_min",
        ("cj", "cps"), ("stuck_days_min",),
    ),
    "stage_changed_after": Filter(
        "stage_changed_after",
        (
            "Pipeline stage was set (cps.created_at) on or AFTER this "
            "ISO date (YYYY-MM-DD). Use for 'stage changes since last "
            "week' style queries."
        ),
        "cps.created_at >= :stage_changed_after",
        ("cj", "cps"), ("stage_changed_after",),
    ),
    "stage_changed_before": Filter(
        "stage_changed_before",
        (
            "Pipeline stage was set (cps.created_at) on or BEFORE this "
            "ISO date."
        ),
        "cps.created_at <= :stage_changed_before",
        ("cj", "cps"), ("stage_changed_before",),
    ),
    "role_name": Filter(
        "role_name",
        "Filter to users with this role (case-insensitive).",
        "LOWER(r.name) = LOWER(:role_name)",
        ("u", "r"), ("role_name",),
    ),
    "role_names": Filter(
        "role_names",
        "Restrict to a list of roles.",
        "r.name IN :role_names",
        ("u", "r"), ("role_names",),
    ),
    "user_enabled": Filter(
        "user_enabled",
        "Boolean — only enabled (True) or disabled (False) users.",
        "u.enable = :user_enabled",
        ("u",), ("user_enabled",),
    ),
    "department": Filter(
        "department",
        "Filter teams by department (case-insensitive exact match).",
        "LOWER(t.department) = LOWER(:department)",
        ("t",), ("department",),
    ),
    "departments": Filter(
        "departments",
        "Restrict to a list of departments.",
        "t.department IN :departments",
        ("t",), ("departments",),
    ),
    "team_status": Filter(
        "team_status",
        "Filter by teams.status — 'active' / 'inactive' (case-insensitive).",
        "LOWER(t.status) = LOWER(:team_status)",
        ("t",), ("team_status",),
    ),
    "team_active": Filter(
        "team_active",
        (
            "Convenience: True → status='active' AND deleted_at IS NULL; "
            "False → otherwise."
        ),
        (
            "(CASE WHEN t.deleted_at IS NULL "
            "AND LOWER(t.status) = 'active' THEN 1 ELSE 0 END) "
            "= CASE WHEN :team_active THEN 1 ELSE 0 END"
        ),
        ("t",), ("team_active",),
    ),
    "deadline_before": Filter(
        "deadline_before",
        "Jobs whose deadline is on or before this ISO date (YYYY-MM-DD).",
        "j.deadline <= :deadline_before",
        ("j",), ("deadline_before",),
    ),
    "deadline_after": Filter(
        "deadline_after",
        "Jobs whose deadline is on or after this ISO date (YYYY-MM-DD).",
        "j.deadline >= :deadline_after",
        ("j",), ("deadline_after",),
    ),
    "location": Filter(
        "location", "Restrict to one job location (case-sensitive match).",
        "j.location = :location",
        ("j",), ("location",),
    ),
    "locations": Filter(
        "locations", "Restrict to a list of job locations.",
        "j.location IN :locations",
        ("j",), ("locations",),
    ),
    "work_mode": Filter(
        "work_mode",
        "Job work-mode filter — ONSITE / REMOTE / HYBRID.",
        "UPPER(j.work_mode) = UPPER(:work_mode)",
        ("j",), ("work_mode",),
    ),
    "work_modes": Filter(
        "work_modes",
        "Restrict to a list of work modes (ONSITE / REMOTE / HYBRID).",
        "UPPER(j.work_mode) IN :work_modes",
        ("j",), ("work_modes",),
    ),
    "candidate_location": Filter(
        "candidate_location",
        "Restrict to one candidate location (current_location, exact match).",
        "c.current_location = :candidate_location",
        ("cj", "c"), ("candidate_location",),
    ),
    "candidate_locations": Filter(
        "candidate_locations",
        "Restrict to a list of candidate locations.",
        "c.current_location IN :candidate_locations",
        ("cj", "c"), ("candidate_locations",),
    ),
    "experience_min": Filter(
        "experience_min",
        "Minimum years of experience (inclusive).",
        "c.experience >= :experience_min",
        ("cj", "c"), ("experience_min",),
    ),
    "experience_max": Filter(
        "experience_max",
        "Maximum years of experience (inclusive).",
        "c.experience <= :experience_max",
        ("cj", "c"), ("experience_max",),
    ),
    "current_salary_min": Filter(
        "current_salary_min",
        "Minimum current_salary (inclusive). Currency-agnostic.",
        "c.current_salary >= :current_salary_min",
        ("cj", "c"), ("current_salary_min",),
    ),
    "current_salary_max": Filter(
        "current_salary_max",
        "Maximum current_salary (inclusive).",
        "c.current_salary <= :current_salary_max",
        ("cj", "c"), ("current_salary_max",),
    ),
    "expected_salary_min": Filter(
        "expected_salary_min",
        "Minimum expected_salary (inclusive).",
        "c.expected_salary >= :expected_salary_min",
        ("cj", "c"), ("expected_salary_min",),
    ),
    "expected_salary_max": Filter(
        "expected_salary_max",
        "Maximum expected_salary (inclusive).",
        "c.expected_salary <= :expected_salary_max",
        ("cj", "c"), ("expected_salary_max",),
    ),
    "on_notice": Filter(
        "on_notice",
        "Boolean — only candidates currently serving notice (True) or not (False).",
        "c.on_notice = :on_notice",
        ("cj", "c"), ("on_notice",),
    ),
    "available_before": Filter(
        "available_before",
        "Candidates available_from on or before this ISO date (joinable sooner).",
        "c.available_from <= :available_before",
        ("cj", "c"), ("available_before",),
    ),
    "available_after": Filter(
        "available_after",
        "Candidates available_from on or after this ISO date.",
        "c.available_from >= :available_after",
        ("cj", "c"), ("available_after",),
    ),
    "profile_source": Filter(
        "profile_source",
        "Restrict by sourcing channel (LinkedIn / Naukri / Referral / etc.).",
        "c.profile_source = :profile_source",
        ("cj", "c"), ("profile_source",),
    ),
    "profile_sources": Filter(
        "profile_sources",
        "Restrict to a list of sourcing channels.",
        "c.profile_source IN :profile_sources",
        ("cj", "c"), ("profile_sources",),
    ),
    "employment_status": Filter(
        "employment_status",
        "Filter by candidates.employment_status (Active / On Notice / …).",
        "c.employment_status = :employment_status",
        ("cj", "c"), ("employment_status",),
    ),
    "current_company": Filter(
        "current_company",
        "Filter by candidates.current_company (exact match).",
        "c.current_company = :current_company",
        ("cj", "c"), ("current_company",),
    ),
    "skill_like": Filter(
        "skill_like",
        (
            "Substring search on the candidates.skills free-text column "
            "(case-insensitive). Use for quick 'has Python' style queries; "
            "for normalized matches use `skill_name`."
        ),
        "LOWER(c.skills) LIKE LOWER(CONCAT('%', :skill_like, '%'))",
        ("cj", "c"), ("skill_like",),
    ),
    "skill_name": Filter(
        "skill_name",
        (
            "Exact (case-insensitive) match against any row in "
            "resume_skills.skill_name on the candidate's resumes."
        ),
        (
            "EXISTS (SELECT 1 FROM resume_personal_details rp2 "
            "JOIN resume_skills rs2 ON rs2.resume_id = rp2.resume_id "
            "WHERE rp2.candidate_id = cj.candidate_id "
            "AND UPPER(rs2.skill_name) = UPPER(:skill_name))"
        ),
        ("cj",), ("skill_name",),
    ),
    "match_score_min": Filter(
        "match_score_min",
        "Minimum overall_match_score (joins resume_matching).",
        "rm.overall_match_score >= :match_score_min",
        ("cj", "rm"), ("match_score_min",),
    ),
}


# ─── Query builder ────────────────────────────────────────────────────

@dataclass
class QuerySpec:
    measure: str
    dimensions: List[str] = field(default_factory=list)
    filters: Dict[str, Any] = field(default_factory=dict)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    limit: int = 50
    order_by: Optional[str] = None    # measure | <dimension name> | None
    order_dir: str = "desc"


class SemanticError(Exception):
    """Raised when the spec references unknown measures / dimensions /
    filters or breaks ACL. The query_data tool wraps this as a
    structured error returned to the model."""


def _required_aliases(spec: QuerySpec, anchor: str) -> List[str]:
    """Aliases we must JOIN to satisfy the spec, given the anchor."""
    needed = {anchor}
    if spec.measure not in MEASURES:
        raise SemanticError(
            f"Unknown measure {spec.measure!r}. "
            f"Available: {sorted(MEASURES)}"
        )
    needed.update(MEASURES[spec.measure].requires_aliases)
    for dim in spec.dimensions:
        if dim not in DIMENSIONS:
            raise SemanticError(
                f"Unknown dimension {dim!r}. "
                f"Available: {sorted(DIMENSIONS)}"
            )
        needed.update(DIMENSIONS[dim].requires_aliases)
    for fkey in spec.filters.keys():
        if fkey not in FILTERS:
            raise SemanticError(
                f"Unknown filter {fkey!r}. Available: {sorted(FILTERS)}"
            )
        needed.update(FILTERS[fkey].requires_aliases)
    return sorted(needed)


def _inner_join_aliases(spec: QuerySpec, anchor: str) -> set:
    """Aliases that must be INNER JOIN (presence required)."""
    inner: set = set()
    for dim in spec.dimensions:
        d = DIMENSIONS[dim]
        inner.update(d.inner_join_aliases)
    # Filters that target aliases beyond the anchor force their joins
    # to be INNER (otherwise the WHERE filter on a LEFT-joined side
    # eliminates rows the wrong way). Stage / outcome / terminal filters
    # are the common cases.
    for fkey in spec.filters.keys():
        f = FILTERS[fkey]
        for a in f.requires_aliases:
            if a != anchor:
                inner.add(a)
    return inner


def _build_join_clause(anchor: str, needed: List[str], inner: set) -> str:
    """Walk the join graph from `anchor` to every needed alias, building
    the FROM/JOIN clause. Every traversed edge becomes one JOIN line —
    INNER if its target alias is in `inner`, LEFT otherwise."""
    placed = {anchor}
    lines: List[str] = []
    for target in needed:
        if target in placed:
            continue
        path = join_path(anchor, target)
        if path is None:
            raise SemanticError(
                f"No join path from {anchor} to {target}; "
                f"check JOIN_GRAPH in schema_meta.py"
            )
        for (a, b) in path:
            if b in placed:
                continue
            edge_sql = JOIN_GRAPH.get((a, b))
            if not edge_sql:
                raise SemanticError(f"Missing join edge {a}->{b}")
            kind = "JOIN" if b in inner else "LEFT JOIN"
            lines.append(f"{kind} {edge_sql}")
            placed.add(b)
    return "\n          ".join(lines)


def _scope_where(scope: CallerScope, anchor: str) -> Tuple[str, Dict[str, Any]]:
    """Return the WHERE fragment + params that enforce the caller's
    visibility. Admins → empty. Recruiters → restricted to their
    assigned jobs, expressed in terms of whichever alias the anchor
    guarantees is present."""
    if scope.unscoped:
        return "", {}
    predicate = _SCOPE_PREDICATE.get(anchor)
    if predicate is None:
        # Unknown anchor — refuse rather than silently skip ACL.
        raise SemanticError(f"No scope predicate for anchor {anchor!r}")
    # User and Team anchors use CallerScope.user_id, not the job_ids set.
    if anchor in ("u", "t"):
        return predicate, {"scope_self_user_id": scope.user_id}
    if not scope.job_ids:
        # Empty assignment → match nothing on this anchor.
        if anchor == "cj":
            return "cj.job_id IN (-1)", {}
        if anchor == "j":
            return "j.id IN (-1)", {}
        if anchor == "co":
            return "co.id IN (-1)", {}
        if anchor == "pl":
            return "pl.id IN (-1)", {}
    return predicate, {"scope_jobs": list(scope.job_ids)}


def _alias_to_table_name(alias: str) -> Tuple[str, str]:
    """Look up the underlying table name + alias for the given alias."""
    for t in TABLES.values():
        if t.alias == alias:
            return t.name, t.alias
    raise SemanticError(f"Unknown anchor alias {alias!r}")


def build_sql(spec: QuerySpec, scope: CallerScope) -> Tuple[str, Dict[str, Any], List[str]]:
    """Compose SQL + bind params + the list of expanding-IN keys.

    Anchor table is read from the measure (`Measure.anchor`). Date
    range filters apply to the anchor's natural date column
    (`ANCHOR_DATE_COL`). Any filter whose value is a list/tuple/set
    automatically gets registered as an expanding-IN bind param.
    """
    measure = MEASURES[spec.measure]
    anchor = measure.anchor
    dims = [DIMENSIONS[d] for d in spec.dimensions]

    needed = _required_aliases(spec, anchor)
    inner = _inner_join_aliases(spec, anchor)
    join_clause = _build_join_clause(anchor, needed, inner)

    # SELECT
    select_fragments: List[str] = []
    for d in dims:
        select_fragments.append(f"{d.sql} AS {d.name}")
    select_fragments.append(f"{measure.sql} AS {measure.name}")
    select_clause = ",\n               ".join(select_fragments)

    # WHERE
    where_parts: List[str] = ["1=1"]
    params: Dict[str, Any] = {}
    expanding: List[str] = []

    for fkey, fval in spec.filters.items():
        f = FILTERS[fkey]
        where_parts.append(f.sql_template)
        if len(f.params) == 1:
            params[f.params[0]] = fval
        else:
            if not isinstance(fval, dict):
                raise SemanticError(
                    f"Filter {fkey!r} expects dict of params {f.params}"
                )
            for p in f.params:
                params[p] = fval[p]

    # Date range — applied to the anchor's natural date column.
    date_col = ANCHOR_DATE_COL.get(anchor)
    if date_col and not date_col.endswith(".id"):
        if spec.date_from:
            where_parts.append(f"{date_col} >= :date_from")
            params["date_from"] = spec.date_from
        if spec.date_to:
            where_parts.append(f"{date_col} <= :date_to")
            params["date_to"] = spec.date_to

    scope_where, scope_params = _scope_where(scope, anchor)
    if scope_where:
        where_parts.append(scope_where)
        params.update(scope_params)

    where_clause = " AND ".join(where_parts)

    # Auto-detect expanding-IN bind params: any list/tuple/set value.
    for k, v in params.items():
        if isinstance(v, (list, tuple, set)):
            expanding.append(k)

    # GROUP BY + ORDER BY
    group_clause = ""
    order_clause = ""
    if dims:
        group_clause = "GROUP BY " + ", ".join(d.sql for d in dims)
        if spec.order_by == "measure" or spec.order_by is None:
            direction = "DESC" if spec.order_dir.lower() == "desc" else "ASC"
            order_clause = f"ORDER BY {measure.name} {direction}"
        elif spec.order_by in {d.name for d in dims}:
            d = next(x for x in dims if x.name == spec.order_by)
            order_clause = f"ORDER BY {d.order_sql or d.sql}"
        else:
            raise SemanticError(
                f"Unknown order_by {spec.order_by!r}. Use 'measure' or one "
                f"of the requested dimensions: {[d.name for d in dims]}"
            )

    anchor_name, anchor_alias = _alias_to_table_name(anchor)
    sql = (
        f"SELECT {select_clause}\n"
        f"          FROM {anchor_name} {anchor_alias}\n"
        f"          {join_clause}\n"
        f"         WHERE {where_clause}\n"
        f"         {group_clause}\n"
        f"         {order_clause}\n"
        f"         LIMIT :_limit"
    )
    params["_limit"] = int(spec.limit)
    return sql, params, expanding


def execute(spec: QuerySpec, mcp: McpClient, scope: CallerScope) -> Dict[str, Any]:
    sql, params, expanding = build_sql(spec, scope)
    rows = mcp.query(sql, params, expanding_keys=expanding or None)
    return {
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
        "sql": sql,   # surfaced for audit; never shown to end users.
    }


# ─── Discovery helpers ────────────────────────────────────────────────

def measures_catalog() -> List[Dict[str, str]]:
    return [{"name": m.name, "description": m.description}
            for m in MEASURES.values()]


def dimensions_catalog() -> List[Dict[str, str]]:
    return [{"name": d.name, "description": d.description}
            for d in DIMENSIONS.values()]


def filters_catalog() -> List[Dict[str, Any]]:
    return [{"name": f.name, "description": f.description,
             "params": list(f.params)}
            for f in FILTERS.values()]
