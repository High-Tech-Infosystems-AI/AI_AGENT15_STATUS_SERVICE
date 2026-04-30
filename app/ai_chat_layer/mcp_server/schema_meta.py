"""Single source of truth for the recruitment-database schema we expose
to the AI agent.

Two artifacts live here:

  TABLES   — declarative metadata about each table the agent reads.
             Used by `describe_schema` to surface the catalog to the
             model and by the semantic layer to build SQL.

  JOINS    — directed edges between table aliases with the SQL clause
             needed to traverse them. The semantic layer's query
             builder walks this graph to compute the minimum set of
             joins for a given (measure, dimensions, filters) tuple.

When the underlying schema drifts (a column rename, a new pipeline
table, a swapped foreign key), the only edit needed is here. Every
tool that touches the DB consumes these constants instead of
hardcoding column names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    type_hint: str       # "int", "string", "datetime", "enum:..."
    description: str
    pii: bool = False    # block from `describe_schema` output for non-admins


@dataclass(frozen=True)
class TableMeta:
    name: str            # actual DB table name
    alias: str           # short alias used in SQL (e.g. "cj")
    description: str
    columns: List[ColumnMeta]
    primary_key: str = "id"

    def col(self, name: str) -> ColumnMeta:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name} has no column {name!r}")


# ─── Table catalog ────────────────────────────────────────────────────

TABLES: Dict[str, TableMeta] = {
    "job_openings": TableMeta(
        name="job_openings", alias="j",
        description="One row per job requisition.",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("job_id", "string", "Public string slug (e.g. JOB_ID_...)"),
            ColumnMeta("title", "string", "Job title"),
            ColumnMeta("status", "enum:ACTIVE|CLOSED|ON_HOLD|...", "Lifecycle status"),
            ColumnMeta("stage", "string", "Free-form coarse stage label"),
            ColumnMeta("openings", "int", "Number of openings"),
            ColumnMeta("location", "string", "Job location"),
            ColumnMeta("work_mode", "string", "ONSITE|REMOTE|HYBRID"),
            ColumnMeta("deadline", "datetime", "Hiring deadline"),
            ColumnMeta("company_id", "int", "FK companies.id"),
            ColumnMeta("pipeline_id", "int", "FK pipelines.id — the funnel template"),
            ColumnMeta("created_at", "datetime", "Created timestamp"),
        ],
    ),
    "candidates": TableMeta(
        name="candidates", alias="c",
        primary_key="candidate_id",
        description=(
            "One row per candidate (PII source of truth). Rich profile "
            "fields — experience / salary / location / availability / "
            "source — drive most candidate analytics. Always loaded as a "
            "full bundle when callers ask for analysis on a candidate."
        ),
        columns=[
            ColumnMeta("candidate_id", "string", "String PK"),
            ColumnMeta("candidate_name", "string", "Display name", pii=True),
            ColumnMeta("candidate_email", "string", "Email", pii=True),
            ColumnMeta("candidate_phone_number", "string", "Phone number", pii=True),
            ColumnMeta("candidate_linkedIn", "string", "LinkedIn URL", pii=True),
            ColumnMeta("portfolio", "string", "Portfolio URL", pii=True),
            ColumnMeta("employment_status", "string", "Active / On Notice / etc."),
            ColumnMeta("employment_type", "string", "Full-time / Contract / Intern / …"),
            ColumnMeta("current_work_mode", "string", "ONSITE / REMOTE / HYBRID"),
            ColumnMeta("work_mode_prefer", "string", "Preferred work mode"),
            ColumnMeta("experience", "decimal", "Years of experience"),
            ColumnMeta("current_company", "string", "Current employer"),
            ColumnMeta("current_location", "string", "Current city / region"),
            ColumnMeta("home_town", "string", "Home town"),
            ColumnMeta("preferred_location", "string", "Preferred work location"),
            ColumnMeta("current_salary", "decimal", "Current annual salary"),
            ColumnMeta("current_salary_curr", "string", "Currency code for current_salary"),
            ColumnMeta("expected_salary", "decimal", "Expected salary"),
            ColumnMeta("expected_salary_curr", "string", "Currency code for expected_salary"),
            ColumnMeta("on_notice", "bool", "True if currently serving notice period"),
            ColumnMeta("available_from", "date", "Earliest joining date"),
            ColumnMeta("year_of_graduation", "int", "Graduation year"),
            ColumnMeta("dob", "date", "Date of birth", pii=True),
            ColumnMeta("age", "int", "Age in years"),
            ColumnMeta("gender", "string", "Gender", pii=True),
            ColumnMeta("skills", "string", "Free-form skills text (comma-separated)"),
            ColumnMeta("industries_worked_on", "string", "Free-form list of industries"),
            ColumnMeta("employment_gap", "string", "Description of any employment gap"),
            ColumnMeta("profile_source", "string", "Sourcing channel — LinkedIn / Naukri / Referral / etc."),
            ColumnMeta("creation_source", "string", "How the record entered the system"),
            ColumnMeta("job_profile", "string", "Functional profile / domain"),
            ColumnMeta("assigned_to", "int", "FK users.id of the recruiter who owns this profile"),
            ColumnMeta("created_by", "int", "FK users.id"),
            ColumnMeta("created_at", "datetime", "Profile creation timestamp"),
            ColumnMeta("updated_at", "datetime", "Profile last-update timestamp"),
        ],
    ),
    "candidate_jobs": TableMeta(
        name="candidate_jobs", alias="cj",
        description="Junction: a candidate's application to one job. Central fact table.",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("candidate_id", "string", "FK candidates.candidate_id"),
            ColumnMeta("job_id", "int", "FK job_openings.id"),
            ColumnMeta("applied_at", "datetime", "Application timestamp"),
        ],
    ),
    "candidate_pipeline_status": TableMeta(
        name="candidate_pipeline_status", alias="cps",
        description=(
            "Pipeline-stage history per (candidate, job). Multiple rows "
            "per candidate_job; `latest=1` is the current row. Always "
            "filter on `cps.latest = 1` for current-state queries."
        ),
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("candidate_job_id", "int", "FK candidate_jobs.id"),
            ColumnMeta("pipeline_stage_id", "int", "FK pipeline_stages.id"),
            ColumnMeta("status", "string", "Free-form option text (matches pipeline_stage_status.option)"),
            ColumnMeta("latest", "tinyint", "1 = current stage row"),
            ColumnMeta("created_at", "datetime", "When this stage was set"),
            ColumnMeta("created_by", "int", "FK users.id of the recruiter who moved them"),
        ],
    ),
    "pipeline_stages": TableMeta(
        name="pipeline_stages", alias="ps",
        description="Stages on a pipeline (e.g. Sourced, Lined Up, Joined).",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("name", "string", "Stage label"),
            ColumnMeta("description", "string", "Free-form description"),
            ColumnMeta("color_code", "string", "Hex color for UI badge"),
            ColumnMeta("order", "int", "Sort order in the pipeline"),
            ColumnMeta("end_stage", "tinyint", "1 = terminal stage"),
            ColumnMeta("pipeline_id", "int", "FK pipelines.id"),
        ],
    ),
    "pipeline_stage_status": TableMeta(
        name="pipeline_stage_status", alias="pss",
        description=(
            "Per-stage status options + their tag (Sourcing / Screening / "
            "LineUps / TurnUps / Selected / OfferReleased / OfferAccepted). "
            "Joined via pipeline_stage_id + UPPER(pss.option) = UPPER(cps.status)."
        ),
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("pipeline_stage_id", "int", "FK pipeline_stages.id"),
            ColumnMeta("option", "string", "Status text shown to user"),
            ColumnMeta("tag", "enum", "Cross-stage category"),
            ColumnMeta("color_code", "string", "Hex color for UI badge"),
            ColumnMeta("order", "int", "Sort order"),
        ],
    ),
    "candidate_job_status": TableMeta(
        name="candidate_job_status", alias="cjs",
        description="Terminal outcomes — `type` is JOINED / REJECTED / DROPPED.",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("candidate_job_id", "int", "FK candidate_jobs.id"),
            ColumnMeta("type", "enum:joined|rejected|dropped", "Terminal status"),
            ColumnMeta("created_at", "datetime", "When set"),
        ],
    ),
    "user_jobs_assigned": TableMeta(
        name="user_jobs_assigned", alias="uja",
        description="Junction: which user (recruiter) is assigned to a job.",
        columns=[
            ColumnMeta("user_id", "int", "FK users.id"),
            ColumnMeta("job_id", "int", "FK job_openings.id"),
        ],
    ),
    "users": TableMeta(
        name="users", alias="u",
        description="Platform users (recruiters, admins, super_admin).",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("name", "string", "Display name", pii=True),
            ColumnMeta("username", "string", "Unique login handle"),
            ColumnMeta("email", "string", "Email", pii=True),
            ColumnMeta("employee_id", "string", "External HRIS employee id"),
            ColumnMeta("role_id", "int", "FK roles.id"),
            ColumnMeta("enable", "tinyint", "1 = active"),
            ColumnMeta("created_at", "datetime", "Account creation timestamp"),
            ColumnMeta("updated_at", "datetime", "Last update"),
            ColumnMeta("deleted_at", "datetime", "Soft-delete tombstone (NULL = live)"),
        ],
    ),
    "roles": TableMeta(
        name="roles", alias="r",
        description="Role catalog (super_admin, admin, user).",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("name", "string", "Role name"),
        ],
    ),
    "teams": TableMeta(
        name="teams", alias="t",
        description=(
            "Team / squad — a group of users that can be collectively "
            "assigned to jobs (via job_team_assignments). Members + "
            "managers live in team_members. `status` is 'active' / "
            "'inactive'; `deleted_at IS NOT NULL` = soft-deleted."
        ),
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("name", "string", "Team display name"),
            ColumnMeta("description", "string", "Free-form description"),
            ColumnMeta("department", "string", "Owning department"),
            ColumnMeta("status", "enum:active|inactive", "Lifecycle status"),
            ColumnMeta("created_at", "datetime", "Created timestamp"),
            ColumnMeta("created_by", "int", "FK users.id (creator)"),
            ColumnMeta("updated_at", "datetime", "Last update"),
            ColumnMeta("deleted_at", "datetime", "Soft-delete tombstone (NULL = live)"),
        ],
    ),
    "team_members": TableMeta(
        name="team_members", alias="tm",
        description="Junction: user ⇄ team membership.",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("team_id", "int", "FK teams.id"),
            ColumnMeta("user_id", "int", "FK users.id"),
            ColumnMeta("role_in_team", "enum:manager|member", "Member's role on this team"),
            ColumnMeta("assigned_at", "datetime", "When the membership was created"),
            ColumnMeta("assigned_by", "int", "FK users.id (who added them)"),
        ],
    ),
    "job_team_assignments": TableMeta(
        name="job_team_assignments", alias="jta",
        description=(
            "Direct team↔job assignment. The CANONICAL way teams are "
            "linked to jobs (distinct from individual team-member "
            "assignments in user_jobs_assigned). One row per (job, team)."
        ),
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("job_id", "int", "FK job_openings.id"),
            ColumnMeta("team_id", "int", "FK teams.id"),
            ColumnMeta("assigned_at", "datetime", "When the team was assigned"),
            ColumnMeta("assigned_by", "int", "FK users.id (who assigned)"),
        ],
    ),
    "companies": TableMeta(
        name="companies", alias="co",
        description="Hiring company / client.",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("company_name", "string", "Company display name"),
        ],
    ),
    "pipelines": TableMeta(
        name="pipelines", alias="pl",
        description="Pipeline templates — parent of pipeline_stages.",
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("pipeline_id", "string", "Public string slug"),
            ColumnMeta("name", "string", "Pipeline display name"),
            ColumnMeta("remarks", "string", "Free-form notes"),
            ColumnMeta("created_at", "datetime", "Created timestamp"),
            ColumnMeta("created_by", "int", "FK users.id"),
            ColumnMeta("updated_at", "datetime", "Last update"),
            ColumnMeta("deleted_at", "datetime", "Soft-delete tombstone (NULL = live)"),
        ],
    ),
    "resume_personal_details": TableMeta(
        name="resume_personal_details", alias="rp",
        primary_key="resume_id",
        description=(
            "Resume header per (candidate, version). Multiple versions "
            "may exist per candidate; latest version = MAX(resume_version)."
        ),
        columns=[
            ColumnMeta("resume_id", "int", "Integer PK"),
            ColumnMeta("candidate_id", "string", "FK candidates.candidate_id"),
            ColumnMeta("resume_version", "int", "Version number (latest = MAX)"),
            ColumnMeta("name", "string", "Name as parsed from resume", pii=True),
            ColumnMeta("email_id", "string", "Email from resume", pii=True),
            ColumnMeta("ph_number", "string", "Phone from resume", pii=True),
            ColumnMeta("address", "string", "Address from resume", pii=True),
            ColumnMeta("languages", "string", "Languages spoken"),
            ColumnMeta("summary", "string", "Resume summary / objective"),
            ColumnMeta("dob", "string", "Date of birth as text", pii=True),
            ColumnMeta("webpage", "string", "Personal webpage / portfolio URL"),
            ColumnMeta("created_at", "datetime", "When the resume was parsed"),
        ],
    ),
    "resume_matching": TableMeta(
        name="resume_matching", alias="rm",
        primary_key="resume_id",
        description=(
            "JD↔resume match analysis per (candidate, version). Holds "
            "skill / qualification / experience / designation match "
            "percentages and the overall_match_score."
        ),
        columns=[
            ColumnMeta("resume_id", "int", "Integer PK"),
            ColumnMeta("candidate_id", "string", "FK candidates.candidate_id"),
            ColumnMeta("resume_version", "int", "Version number"),
            ColumnMeta("overall_match_score", "decimal", "Composite 0–100 match score"),
            ColumnMeta("skills_match_percentage", "decimal", "Skill overlap %"),
            ColumnMeta("qualification_match_percentage", "decimal", "Qualification %"),
            ColumnMeta("experience_match_percentage", "decimal", "Experience %"),
            ColumnMeta("designation_match_percentage", "decimal", "Designation %"),
            ColumnMeta("matched_skills", "string", "Skills present in both JD and resume"),
            ColumnMeta("missing_skills", "string", "JD skills missing from resume"),
            ColumnMeta("extra_skills", "string", "Resume skills not in JD"),
            ColumnMeta("recommendation", "string", "AI recommendation text"),
            ColumnMeta("created_at", "datetime", "When the match was computed"),
        ],
    ),
    "resume_skills": TableMeta(
        name="resume_skills", alias="rs",
        primary_key="skill_id",
        description=(
            "Per-resume normalized skill rows. Joined via resume_id; "
            "one row per skill mention with category technical / soft / other."
        ),
        columns=[
            ColumnMeta("skill_id", "int", "Integer PK"),
            ColumnMeta("resume_id", "int", "FK resume_personal_details.resume_id"),
            ColumnMeta("skill_category", "enum:technical|soft|other", "Skill class"),
            ColumnMeta("skill_name", "string", "Skill text"),
        ],
    ),
    "candidate_status": TableMeta(
        name="candidate_status", alias="cs",
        description=(
            "Single latest free-form status per candidate (one row per "
            "candidate, replaced on update). Use for 'what is this "
            "candidate's status' questions; pipeline state lives in "
            "candidate_pipeline_status instead."
        ),
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("candidate_id", "string", "FK candidates.candidate_id (unique)"),
            ColumnMeta("candidate_status", "string", "Free-form status label"),
            ColumnMeta("remarks", "string", "Free-form remarks"),
            ColumnMeta("updated_at", "datetime", "Last update"),
        ],
    ),
    "candidate_activity": TableMeta(
        name="candidate_activity", alias="ca",
        description=(
            "Per-event remark / activity log per candidate. Multiple rows "
            "per candidate ordered by created_at DESC for a timeline. "
            "type ∈ {general, pipeline, accepted, status, rejected}."
        ),
        columns=[
            ColumnMeta("id", "int", "Integer PK"),
            ColumnMeta("candidate_id", "string", "FK candidates.candidate_id"),
            ColumnMeta("user_id", "int", "Actor (FK users.id)"),
            ColumnMeta("remark", "string", "Free-form note"),
            ColumnMeta("type", "enum:general|pipeline|accepted|status|rejected", "Event class"),
            ColumnMeta("key_id", "string", "Optional reference id (e.g. job_id) the event ties to"),
            ColumnMeta("created_at", "datetime", "Event time"),
        ],
    ),
}


# ─── Join graph ───────────────────────────────────────────────────────
#
# Each entry is `(from_alias, to_alias) -> SQL fragment`. The fragment
# is the part AFTER the JOIN keyword (i.e. starts with table name). The
# semantic layer prepends "LEFT JOIN" by default; if a join MUST be
# inner (so a row presence is required, e.g. cps for stage filters),
# the consuming dimension/filter declares that.

JOIN_GRAPH: Dict[Tuple[str, str], str] = {
    # ── candidate_jobs ⇄ job_openings ──
    ("cj", "j"):
        "job_openings j ON j.id = cj.job_id",
    ("j", "cj"):
        "candidate_jobs cj ON cj.job_id = j.id",

    # ── job_openings ⇄ companies ──
    ("j", "co"):
        "companies co ON co.id = j.company_id",
    ("co", "j"):
        "job_openings j ON j.company_id = co.id",

    # ── candidate_jobs ⇄ candidates ──
    ("cj", "c"):
        "candidates c ON c.candidate_id = cj.candidate_id",
    ("c", "cj"):
        "candidate_jobs cj ON cj.candidate_id = c.candidate_id",

    # ── candidate_jobs / job_openings → user_jobs_assigned → users ──
    ("cj", "uja"):
        "user_jobs_assigned uja ON uja.job_id = cj.job_id",
    ("j", "uja"):
        "user_jobs_assigned uja ON uja.job_id = j.id",
    ("uja", "u"):
        "users u ON u.id = uja.user_id",

    # ── users → roles, teams ──
    ("u", "r"):
        "roles r ON r.id = u.role_id",
    ("u", "tm"):
        "team_members tm ON tm.user_id = u.id",
    ("tm", "t"):
        "teams t ON t.id = tm.team_id",
    ("t", "tm"):
        "team_members tm ON tm.team_id = t.id",

    # ── job_openings ⇄ teams (canonical via job_team_assignments) ──
    ("j", "jta"):
        "job_team_assignments jta ON jta.job_id = j.id",
    ("jta", "t"):
        "teams t ON t.id = jta.team_id",
    ("t", "jta"):
        "job_team_assignments jta ON jta.team_id = t.id",
    ("jta", "j"):
        "job_openings j ON j.id = jta.job_id",

    # ── candidate_jobs → candidate_pipeline_status (latest=1) ──
    ("cj", "cps"):
        "candidate_pipeline_status cps ON cps.candidate_job_id = cj.id AND cps.latest = 1",
    ("cps", "ps"):
        "pipeline_stages ps ON ps.id = cps.pipeline_stage_id",
    ("cps", "pss"):
        ("pipeline_stage_status pss "
         "ON pss.pipeline_stage_id = cps.pipeline_stage_id "
         "AND UPPER(pss.option) = UPPER(cps.status)"),

    # ── job_openings / pipeline_stages → pipelines ──
    # Two reachable paths to pl: directly via j.pipeline_id (preferred,
    # fewer hops for j-anchored queries) and via ps.pipeline_id when a
    # cj-anchored query already pulls in stages.
    ("j", "pl"):
        "pipelines pl ON pl.id = j.pipeline_id",
    ("ps", "pl"):
        "pipelines pl ON pl.id = ps.pipeline_id",

    # ── terminal status ──
    ("cj", "cjs"):
        "candidate_job_status cjs ON cjs.candidate_job_id = cj.id",

    # ── candidate ⇄ resume (latest-version semantics live in measures) ──
    ("c", "rp"):
        "resume_personal_details rp ON rp.candidate_id = c.candidate_id",
    ("cj", "rp"):
        "resume_personal_details rp ON rp.candidate_id = cj.candidate_id",
    ("rp", "rs"):
        "resume_skills rs ON rs.resume_id = rp.resume_id",
    ("rp", "rm"):
        ("resume_matching rm ON rm.candidate_id = rp.candidate_id "
         "AND rm.resume_version = rp.resume_version"),
    ("c", "rm"):
        "resume_matching rm ON rm.candidate_id = c.candidate_id",
    ("cj", "rm"):
        "resume_matching rm ON rm.candidate_id = cj.candidate_id",

    # ── candidate ⇄ activity / status ──
    ("c", "ca"):
        "candidate_activity ca ON ca.candidate_id = c.candidate_id",
    ("cj", "ca"):
        "candidate_activity ca ON ca.candidate_id = cj.candidate_id",
    ("c", "cs"):
        "candidate_status cs ON cs.candidate_id = c.candidate_id",
    ("cj", "cs"):
        "candidate_status cs ON cs.candidate_id = cj.candidate_id",
}


def join_path(start: str, target: str) -> Optional[List[Tuple[str, str]]]:
    """BFS for the shortest path of joins from `start` alias to `target`
    alias. Returns a list of (from, to) edges, or None if no path."""
    if start == target:
        return []
    visited = {start}
    queue: List[Tuple[str, List[Tuple[str, str]]]] = [(start, [])]
    while queue:
        node, path = queue.pop(0)
        for (a, b) in JOIN_GRAPH.keys():
            if a == node and b not in visited:
                new_path = path + [(a, b)]
                if b == target:
                    return new_path
                visited.add(b)
                queue.append((b, new_path))
    return None


def alias_for(table: str) -> str:
    return TABLES[table].alias


def all_aliases() -> List[str]:
    return [t.alias for t in TABLES.values()]
