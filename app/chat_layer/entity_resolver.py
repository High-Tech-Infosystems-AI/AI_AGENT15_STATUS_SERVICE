"""Resolve `(type, id)` pairs into normalized "card" dicts that the chat
frontend renders inline in messages. Also powers the picker search.

We hit the shared MySQL DB directly rather than calling each service's REST
API. All these tables already live in the same database (Recruitment Agent
is a multi-service monolith from a data perspective), and the chat-service
SessionLocal can reach them. This is dramatically simpler than service-to-
service HTTP and avoids a fan-out of timeouts.

Card shape (returned to the frontend):
    {
      "type":        str,   # job | candidate | company | pipeline | user | team | report
      "id":          int|str,
      "title":       str,                # primary label
      "subtitle":    str | None,         # supporting line
      "status":      str | None,         # short status word ("OPEN", "CLOSED", "ACTIVE", …)
      "status_color":str | None,         # tailwind color hint (green/amber/red/gray)
      "deep_link":   str,                # frontend route — used by click-through
      "fields":      [{label, value}]    # optional extra rows ("Owner: Alice", …)
    }
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

ENTITY_TYPES = ("job", "candidate", "company", "pipeline", "user", "team", "report")


def _status_color_for(s: Optional[str]) -> Optional[str]:
    """Map any free-form status to a 4-color palette the FE can render."""
    if not s:
        return None
    v = s.lower()
    if v in {"open", "active", "in_progress", "ongoing", "running"}:
        return "green"
    if v in {"on_hold", "paused", "draft", "pending"}:
        return "amber"
    if v in {"closed", "rejected", "cancelled", "blocked"}:
        return "red"
    return "gray"


# ---------------------------------------------------------------------------
# Per-type resolvers: each takes a DB session and a list of ids, returns a
# list of cards in the SAME ORDER as the requested ids (None for missing).
# ---------------------------------------------------------------------------

def _resolve_jobs(db: Session, ids: List[int]) -> List[Optional[dict]]:
    """Job card. Pulls from `job_openings` joined to `companies` for the
    subtitle, plus two correlated subqueries to surface 'who's working on
    this' (assigned recruiters) and 'how many candidates have joined'."""
    if not ids:
        return []
    rows = db.execute(text("""
        SELECT j.id, j.job_id, j.title, j.status, j.stage, j.deadline,
               j.openings, j.location, j.work_mode,
               c.company_name AS company_name,
               (SELECT COUNT(*) FROM user_jobs_assigned uja
                 WHERE uja.job_id = j.id) AS recruiter_count,
               (SELECT COUNT(*) FROM candidate_jobs cj
                 WHERE cj.job_id = j.id) AS applicant_count
          FROM job_openings j
     LEFT JOIN companies c ON c.id = j.company_id
         WHERE j.id IN :ids
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    by_id = {r._mapping["id"]: r._mapping for r in rows}
    out: List[Optional[dict]] = []
    for jid in ids:
        m = by_id.get(jid)
        if not m:
            out.append(None)
            continue
        fields = [
            {"label": "Openings", "value": str(m["openings"] or 0)},
            {"label": "Applicants", "value": str(m["applicant_count"] or 0)},
        ]
        if m["recruiter_count"]:
            fields.append({"label": "Recruiters", "value": str(m["recruiter_count"])})
        if m["deadline"]:
            fields.append({"label": "Deadline", "value": m["deadline"].isoformat()})
        if m["stage"]:
            fields.append({"label": "Stage", "value": m["stage"]})
        if m["work_mode"]:
            fields.append({"label": "Mode", "value": m["work_mode"]})
        # Click-through:
        #   ACTIVE jobs → the job's pipeline kanban (where work happens).
        #     Note: the kanban route uses the public string `job_id`
        #     (e.g. "JOB_ID_..."), not the integer PK. The FE also needs
        #     the integer to fetch full job details before navigating;
        #     that's exposed via card.id while the route uses external_id.
        #   Anything else (CLOSED, INACTIVE, ON_HOLD, …) → the jobs list,
        #     since the pipeline view isn't useful for a job that's no
        #     longer being worked on.
        status_upper = (m["status"] or "").upper()
        external_id = m["job_id"]
        deep_link = (f"/job-pipeline/{external_id}"
                     if status_upper == "ACTIVE" and external_id else "/jobs")
        out.append({
            "type": "job",
            "id": m["id"],
            "external_id": external_id,
            "title": m["title"] or f"Job {m['id']}",
            "subtitle": m["company_name"] or m["location"] or None,
            "status": status_upper or None,
            "status_color": _status_color_for(m["status"]),
            "deep_link": deep_link,
            "fields": fields,
        })
    return out


def _resolve_candidates(db: Session, ids: list) -> List[Optional[dict]]:
    """Candidate card.

    PK on `candidates` is `candidate_id` (String), so we alias it to `id`
    for resolver-uniform handling. The pill status is taken from the
    latest row of the dedicated `candidate_status` table (Text column),
    which carries the recruiter-curated state. We fall back to the raw
    `employment_status` column when no status row exists yet, and to
    nothing when both are empty.
    """
    if not ids:
        return []
    str_ids = [str(i) for i in ids]
    rows = db.execute(text("""
        SELECT c.candidate_id AS id,
               c.candidate_name, c.candidate_email, c.employment_status,
               c.experience, c.current_company, c.current_location,
               c.job_profile,
               (SELECT cs.candidate_status
                  FROM candidate_status cs
                 WHERE cs.candidate_id = c.candidate_id
              ORDER BY cs.updated_at DESC, cs.id DESC
                 LIMIT 1) AS latest_status,
               (SELECT COUNT(*) FROM candidate_jobs cj
                 WHERE cj.candidate_id = c.candidate_id) AS job_count
          FROM candidates c
         WHERE c.candidate_id IN :ids
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": str_ids},
    ).all()
    by_id = {str(r._mapping["id"]): r._mapping for r in rows}
    out: List[Optional[dict]] = []
    for cid in ids:
        m = by_id.get(str(cid))
        if not m:
            out.append(None)
            continue
        fields = []
        if m["job_profile"]:
            fields.append({"label": "Profile", "value": m["job_profile"]})
        if m["experience"] is not None:
            fields.append({"label": "Experience", "value": f"{m['experience']} yrs"})
        if m["current_company"]:
            fields.append({"label": "Currently at", "value": m["current_company"]})
        if m["current_location"]:
            fields.append({"label": "Location", "value": m["current_location"]})
        if m["job_count"]:
            fields.append({"label": "Applied to", "value": f"{m['job_count']} jobs"})
        # Prefer the curated `candidate_status` text; fall back to
        # `employment_status` (e.g. "Active", "On Notice").
        raw = (m["latest_status"] or "").strip() or (m["employment_status"] or "").strip()
        # Status text can be free-form / multi-line — take the first line
        # and clamp to a 24-char pill so the layout stays clean.
        status = (raw.split("\n", 1)[0][:24]).upper() if raw else None
        out.append({
            "type": "candidate",
            "id": m["id"],
            "title": m["candidate_name"] or f"Candidate {m['id']}",
            "subtitle": m["candidate_email"] or None,
            "status": status,
            "status_color": _status_color_for(raw),
            "deep_link": f"/candidates?id={m['id']}",
            "fields": fields,
        })
    return out


def _resolve_companies(db: Session, ids: List[int]) -> List[Optional[dict]]:
    if not ids:
        return []
    rows = db.execute(text("""
        SELECT id, company_name, location, industry, status, employee_count
          FROM companies
         WHERE id IN :ids
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    by_id = {r._mapping["id"]: r._mapping for r in rows}
    out: List[Optional[dict]] = []
    for cid in ids:
        m = by_id.get(cid)
        if not m:
            out.append(None)
            continue
        fields = []
        if m["industry"]:
            fields.append({"label": "Industry", "value": m["industry"]})
        if m["employee_count"] is not None:
            fields.append({"label": "Employees", "value": str(m["employee_count"])})
        out.append({
            "type": "company",
            "id": m["id"],
            "title": m["company_name"] or f"Company {m['id']}",
            "subtitle": m["location"] or None,
            "status": (m["status"] or "").upper() or None,
            "status_color": _status_color_for(m["status"]),
            "deep_link": f"/companies/detail/{m['id']}",
            "fields": fields,
        })
    return out


def _resolve_pipelines(db: Session, ids: List[int]) -> List[Optional[dict]]:
    if not ids:
        return []
    rows = db.execute(text("""
        SELECT p.id, p.name, p.remarks,
               (SELECT COUNT(*) FROM pipeline_stages s WHERE s.pipeline_id = p.id) AS stage_count
          FROM pipelines p
         WHERE p.id IN :ids
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    by_id = {r._mapping["id"]: r._mapping for r in rows}
    out: List[Optional[dict]] = []
    for pid in ids:
        m = by_id.get(pid)
        if not m:
            out.append(None)
            continue
        out.append({
            "type": "pipeline",
            "id": m["id"],
            "title": m["name"] or f"Pipeline {m['id']}",
            "subtitle": m["remarks"] or None,
            "status": None,
            "status_color": None,
            "deep_link": f"/update-pipeline/{m['id']}",
            "fields": [
                {"label": "Stages", "value": str(m["stage_count"] or 0)},
            ],
        })
    return out


def _resolve_users(db: Session, ids: List[int]) -> List[Optional[dict]]:
    if not ids:
        return []
    rows = db.execute(text("""
        SELECT u.id, u.name, u.username, u.email, u.profile_image_key, u.enable,
               r.name AS role_name
          FROM users u
     LEFT JOIN roles r ON r.id = u.role_id
         WHERE u.id IN :ids AND u.deleted_at IS NULL
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    by_id = {r._mapping["id"]: r._mapping for r in rows}
    out: List[Optional[dict]] = []
    # Profile image presigning is reused from the chat helper so the avatar
    # in the card can use the same short-lived URL flow.
    from app.chat_layer.s3_chat_service import presign_profile_image
    for uid in ids:
        m = by_id.get(uid)
        if not m:
            out.append(None)
            continue
        avatar = presign_profile_image(m.get("profile_image_key"))
        out.append({
            "type": "user",
            "id": m["id"],
            "title": m["name"] or m["username"] or f"User {m['id']}",
            "subtitle": m["email"] or None,
            "status": "ACTIVE" if (m.get("enable") or 0) == 1 else "DISABLED",
            "status_color": "green" if (m.get("enable") or 0) == 1 else "gray",
            "deep_link": "/settings/users",
            "avatar_url": avatar,
            "fields": [
                {"label": "Role", "value": m["role_name"] or "—"},
            ],
        })
    return out


def _resolve_teams(db: Session, ids: List[int]) -> List[Optional[dict]]:
    if not ids:
        return []
    rows = db.execute(text("""
        SELECT t.id, t.name,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id) AS members
          FROM teams t
         WHERE t.id IN :ids
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    by_id = {r._mapping["id"]: r._mapping for r in rows}
    out: List[Optional[dict]] = []
    for tid in ids:
        m = by_id.get(tid)
        if not m:
            out.append(None)
            continue
        out.append({
            "type": "team",
            "id": m["id"],
            "title": m["name"] or f"Team {m['id']}",
            "subtitle": None,
            "status": None,
            "status_color": None,
            "deep_link": "/settings/teams",
            "fields": [
                {"label": "Members", "value": str(m["members"] or 0)},
            ],
        })
    return out


# ---------------------------------------------------------------------------
# Reports catalog — hardcoded mapping over AI_AGENT16_Dashboard_Service's
# /api/charts/* endpoints. Selecting a report creates a reference whose
# deep_link drops the user on /dashboard with the chart pre-selected.
# ---------------------------------------------------------------------------

REPORTS_CATALOG: list[dict] = [
    # Each entry carries:
    #   chart_type: how the FE snapshot component should render the
    #     placeholder visual ("line", "bar", "funnel", "donut", "table").
    #   filters:    list of filter keys the picker should prompt for
    #     before committing the ref. Drives the UI form, not the SQL.
    #     Recognized keys: date_range (date_from + date_to),
    #       granularity (hourly|daily|weekly|monthly|yearly),
    #       company, job, user.
    #
    # `date_range` is implicit-supported by every chart endpoint so we
    # include it almost everywhere; `granularity` only on trend-shaped
    # endpoints.

    # — Featured / DM-friendly reports near the top so they're the
    # first results the picker shows by default.
    {"id": "recruiter-efficiency", "title": "Recruiter Efficiency",
     "subtitle": "Per-recruiter conversion + activity breakdown",
     "chart_type": "bar", "filters": ["date_range", "user"],
     "scope": "any"},

    {"id": "top-recruiters", "title": "Top Recruiters",
     "subtitle": "Ranked recruiter leaderboard",
     "chart_type": "bar", "filters": ["date_range"],
     "scope": "group"},

    {"id": "platform-metrics", "title": "Platform Metrics",
     "subtitle": "Sourcing platform distribution",
     "chart_type": "donut", "filters": ["date_range", "user", "job"]},

    {"id": "ai-distribution", "title": "AI Distribution",
     "subtitle": "AI-assisted activity per recruiter",
     "chart_type": "bar", "filters": ["date_range", "user"]},

    {"id": "pipeline-funnel", "title": "Pipeline Funnel",
     "subtitle": "Stage-by-stage candidate count",
     "chart_type": "funnel", "filters": ["date_range", "company", "job"]},

    {"id": "pipeline-funnel-details", "title": "Pipeline Funnel Details",
     "subtitle": "Drill-down into each funnel stage",
     "chart_type": "table", "filters": ["date_range", "company", "job"]},

    {"id": "hiring-funnel", "title": "Hiring Funnel",
     "subtitle": "Joined vs rejected over time",
     "chart_type": "line", "filters": ["date_range", "granularity", "company", "job"]},

    {"id": "daily-trend", "title": "Daily Trend",
     "subtitle": "Joined / rejected (hourly–yearly)",
     "chart_type": "line", "filters": ["date_range", "granularity"]},

    {"id": "latest-jobs", "title": "Latest Jobs",
     "subtitle": "Most recent job openings",
     "chart_type": "table", "filters": ["date_range"]},

    {"id": "count-jobs", "title": "Job Count",
     "subtitle": "Aggregate open job count",
     "chart_type": "donut", "filters": ["date_range"]},

    {"id": "company-jobs-count", "title": "Jobs by Company",
     "subtitle": "Per-company job distribution",
     "chart_type": "bar", "filters": ["date_range"]},

    {"id": "count-candidates", "title": "Candidate Count",
     "subtitle": "Aggregate candidate count",
     "chart_type": "donut", "filters": ["date_range"]},

    {"id": "user-candidate-share-today", "title": "Recruiter Load Today",
     "subtitle": "Per-recruiter candidate distribution",
     "chart_type": "bar", "filters": []},

    {"id": "hiring-summary-details", "title": "Hiring Summary",
     "subtitle": "Detailed hiring metrics",
     "chart_type": "table", "filters": ["date_range"]},

    {"id": "pipeline-progress-details", "title": "Pipeline Progress",
     "subtitle": "Stage-wise progress breakdown",
     "chart_type": "bar", "filters": ["date_range", "company", "job"]},

    {"id": "daily-performance", "title": "Daily Performance",
     "subtitle": "Recruiter daily performance",
     "chart_type": "line", "filters": ["date_range", "granularity", "user"]},

    {"id": "daily-performance-details", "title": "Daily Performance Details",
     "subtitle": "Drill-down for recruiter daily performance",
     "chart_type": "table", "filters": ["date_range", "user"]},

    {"id": "user-logins-details", "title": "User Logins",
     "subtitle": "Login activity breakdown",
     "chart_type": "table", "filters": ["date_range"]},

    {"id": "avg-time-stages", "title": "Avg Time Per Stage",
     "subtitle": "Average duration per pipeline stage",
     "chart_type": "bar", "filters": ["date_range", "company", "job"]},

    {"id": "pipeline-velocity", "title": "Pipeline Velocity",
     "subtitle": "Candidates moving per day",
     "chart_type": "line", "filters": ["date_range", "granularity"]},

    {"id": "clawback-metrics", "title": "Clawback Metrics",
     "subtitle": "Clawback rates by recruiter",
     "chart_type": "bar", "filters": ["date_range"]},

    {"id": "clawback-details", "title": "Clawback Details",
     "subtitle": "Per-candidate clawback drill-down",
     "chart_type": "table", "filters": ["date_range"]},

    {"id": "clawback-status-graph", "title": "Clawback Status Graph",
     "subtitle": "Clawback status visualization",
     "chart_type": "donut", "filters": ["date_range"]},

    {"id": "company-performance", "title": "Company Performance",
     "subtitle": "Per-company hiring metrics",
     "chart_type": "bar", "filters": ["date_range", "company"]},
]
REPORTS_BY_ID = {r["id"]: r for r in REPORTS_CATALOG}


# Filter keys we pass through into the dashboard URL. Anything else on
# `params` is dropped at deep-link build time so a malformed payload can't
# inject arbitrary query string content.
_REPORT_FILTER_PARAM_KEYS = {
    "date_from", "date_to", "granularity",
    "company_id", "job_id", "user_id",
}


def _build_report_deep_link(report_id: str, params: Optional[dict]) -> str:
    """Build a deterministic /dashboard URL with the report id + filters.
    Empty / unknown keys are silently dropped so callers can pass partial
    `params` without poisoning the URL."""
    qs_parts = [f"chart={report_id}"]
    if params:
        for k, v in params.items():
            if k not in _REPORT_FILTER_PARAM_KEYS:
                continue
            if v is None or v == "":
                continue
            qs_parts.append(f"{k}={v}")
    return "/dashboard?" + "&".join(qs_parts)


def _format_filter_summary(params: Optional[dict]) -> Optional[str]:
    """Render a one-line "Last 30 days · Daily" subtitle for the card."""
    if not params:
        return None
    parts: list[str] = []
    df = params.get("date_from")
    dt = params.get("date_to")
    if df and dt:
        parts.append(f"{df} → {dt}")
    elif df:
        parts.append(f"from {df}")
    elif dt:
        parts.append(f"until {dt}")
    g = params.get("granularity")
    if g:
        parts.append(str(g).capitalize())
    return " · ".join(parts) if parts else None


def _resolve_reports(_db: Session, refs: list) -> List[Optional[dict]]:
    """Reports resolver. Unlike the others this one reads `params` from
    each ref and folds it into the card's deep_link + subtitle. Catalog
    metadata (chart_type, filter spec) flows through to the card so the
    snapshot component on the FE can pick the right template."""
    out: List[Optional[dict]] = []
    for ref in refs:
        rid = ref.get("id") if isinstance(ref, dict) else ref
        params = ref.get("params") if isinstance(ref, dict) else None
        meta = REPORTS_BY_ID.get(rid)
        if not meta:
            out.append(None)
            continue
        subtitle = _format_filter_summary(params) or meta["subtitle"]
        out.append({
            "type": "report",
            "id": meta["id"],
            "title": meta["title"],
            "subtitle": subtitle,
            "status": None,
            "status_color": None,
            "deep_link": _build_report_deep_link(meta["id"], params),
            "fields": [],
            "chart_type": meta.get("chart_type"),
            "params": params or None,
        })
    return out


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

_RESOLVERS = {
    "job":       _resolve_jobs,
    "candidate": _resolve_candidates,
    "company":   _resolve_companies,
    "pipeline":  _resolve_pipelines,
    "user":      _resolve_users,
    "team":      _resolve_teams,
    "report":    _resolve_reports,
}


def resolve(db: Session, refs: Iterable[dict]) -> List[Optional[dict]]:
    """Resolve a heterogeneous list of references in one pass. Preserves
    input order. Unknown types or missing rows return None at that index.

    Reports are dispatched with the full ref dict so per-ref `params`
    (filters) flow through. Other types still receive an id-only list
    since their cards don't vary with extra context.
    """
    refs = list(refs)
    out: List[Optional[dict]] = [None] * len(refs)
    by_type: dict[str, list[tuple[int, dict]]] = {}
    for i, ref in enumerate(refs):
        t = (ref or {}).get("type")
        rid = (ref or {}).get("id")
        if t not in _RESOLVERS or rid is None:
            continue
        by_type.setdefault(t, []).append((i, ref))
    for t, items in by_type.items():
        positions = [i for i, _ in items]
        try:
            if t == "report":
                cards = _resolve_reports(db, [r for _, r in items])
            else:
                ids = [r["id"] for _, r in items]
                cards = _RESOLVERS[t](db, ids)
        except Exception as e:
            logger.exception("entity resolve failed type=%s: %s", t, e)
            cards = [None] * len(items)
        for pos, card in zip(positions, cards):
            out[pos] = card
    return out


# ---------------------------------------------------------------------------
# Picker search — used by the + button modal and inline @-autocomplete.
# Returns up to `limit` results in card-shape.
# ---------------------------------------------------------------------------

ADMIN_ROLES = {"admin", "superadmin", "super_admin", "super admin"}


def is_admin_role(role_name: Optional[str]) -> bool:
    return (role_name or "").strip().lower().replace(" ", "_") in ADMIN_ROLES


def get_user_role_name(db: Session, user_id: int) -> Optional[str]:
    """Resolve a user's role name. Used to decide DM scoping when the
    caller is asking about entities visible to the DM peer."""
    row = db.execute(text(
        "SELECT r.name FROM users u "
        "LEFT JOIN roles r ON r.id = u.role_id "
        "WHERE u.id = :uid"
    ), {"uid": user_id}).first()
    return row[0] if row else None


def search(db: Session, *, type_: str, q: str, limit: int = 12,
           scope_user_id: Optional[int] = None) -> List[dict]:
    """Search a single entity type for the picker.

    `scope_user_id`:
      - `None` → return everything matching the query (admin / unscoped view).
      - `int`  → restrict to entities accessible to that user. Today only
        applies to `company` and `job` types via `user_jobs_assigned`.
        Other types are unaffected.

    The caller (entities_api.search_entities) computes the right value:
      - Team / general / no conversation: scope to the caller (or None for admins).
      - DM with admin peer: None (full access on both sides).
      - DM with regular-user peer: scope to that peer's user_id.
    """
    q = (q or "").strip()
    if type_ not in _RESOLVERS:
        return []
    if type_ == "report":
        ql = q.lower()
        # In a DM scoped to a regular-user peer, hide "group-only"
        # reports (e.g. top-recruiters leaderboard) — they aren't a
        # natural thing to send to one person. The caller (entities_api)
        # signals this by passing scope_user_id.
        catalog = REPORTS_CATALOG
        if scope_user_id is not None:
            catalog = [r for r in catalog if r.get("scope") != "group"]
        # Reports tab is curated, so we don't apply the per-type `limit`
        # the same way as searchable entities — return the whole catalog
        # (capped generously) so newly-added reports always surface even
        # if they sit further down the list.
        report_cap = max(limit, 50)
        if not ql:
            picks = catalog[:report_cap]
        else:
            picks = [r for r in catalog
                     if ql in r["title"].lower()
                     or ql in (r.get("subtitle") or "").lower()][:report_cap]
        # Picker results — no params yet (the user picks filters next).
        # We still merge the catalog metadata into the card so the FE
        # filter step knows which filters to prompt for, and signal
        # which filter should auto-fill to the DM peer when applicable.
        cards = _resolve_reports(db, [{"type": "report", "id": r["id"]}
                                       for r in picks])
        for card, meta in zip(cards, picks):
            if card:
                card["filters_spec"] = list(meta.get("filters") or [])
                if scope_user_id is not None and "user" in (meta.get("filters") or []):
                    card["autofill_user_id"] = scope_user_id
        return [c for c in cards if c]

    if scope_user_id and type_ == "company":
        sql, params = _q_companies_for_user(q, limit, scope_user_id)
    elif scope_user_id and type_ == "job":
        sql, params = _q_jobs_for_user(q, limit, scope_user_id)
    else:
        sql, params = _SEARCH_QUERIES[type_](q, limit)
    rows = db.execute(text(sql), params).all()
    ids = [r._mapping["id"] for r in rows]
    cards = _RESOLVERS[type_](db, ids)
    return [c for c in cards if c]


def _q_jobs(q: str, limit: int):
    if q:
        return ("SELECT id FROM job_openings "
                "WHERE title LIKE :q ORDER BY id DESC LIMIT :lim",
                {"q": f"%{q}%", "lim": limit})
    return ("SELECT id FROM job_openings ORDER BY id DESC LIMIT :lim",
            {"lim": limit})


def _q_candidates(q: str, limit: int):
    # Candidates' primary key is `candidate_id` (String). Alias to `id` so
    # the search dispatcher (which reads `r._mapping["id"]`) stays generic.
    if q:
        return ("SELECT candidate_id AS id FROM candidates "
                "WHERE candidate_name LIKE :q OR candidate_email LIKE :q "
                "ORDER BY created_at DESC LIMIT :lim",
                {"q": f"%{q}%", "lim": limit})
    return ("SELECT candidate_id AS id FROM candidates "
            "ORDER BY created_at DESC LIMIT :lim",
            {"lim": limit})


def _q_companies(q: str, limit: int):
    if q:
        return ("SELECT id FROM companies "
                "WHERE company_name LIKE :q OR location LIKE :q "
                "ORDER BY id DESC LIMIT :lim",
                {"q": f"%{q}%", "lim": limit})
    return ("SELECT id FROM companies ORDER BY id DESC LIMIT :lim",
            {"lim": limit})


def _q_companies_for_user(q: str, limit: int, user_id: int):
    """Companies owning at least one job assigned to `user_id`. Used when
    a non-admin caller opens the company picker — they should only see
    companies they're actively recruiting for."""
    base = ("SELECT DISTINCT c.id FROM companies c "
            "JOIN job_openings j ON j.company_id = c.id "
            "JOIN user_jobs_assigned uja ON uja.job_id = j.id "
            "WHERE uja.user_id = :uid")
    if q:
        return (
            base + " AND (c.company_name LIKE :q OR c.location LIKE :q) "
            "ORDER BY c.id DESC LIMIT :lim",
            {"uid": user_id, "q": f"%{q}%", "lim": limit},
        )
    return (
        base + " ORDER BY c.id DESC LIMIT :lim",
        {"uid": user_id, "lim": limit},
    )


def _q_jobs_for_user(q: str, limit: int, user_id: int):
    """Jobs assigned to `user_id`. Mirrors the company scoping so a
    non-admin caller sees the same slice of work in both pickers."""
    base = ("SELECT j.id FROM job_openings j "
            "JOIN user_jobs_assigned uja ON uja.job_id = j.id "
            "WHERE uja.user_id = :uid")
    if q:
        return (
            base + " AND j.title LIKE :q ORDER BY j.id DESC LIMIT :lim",
            {"uid": user_id, "q": f"%{q}%", "lim": limit},
        )
    return (
        base + " ORDER BY j.id DESC LIMIT :lim",
        {"uid": user_id, "lim": limit},
    )


def _q_pipelines(q: str, limit: int):
    if q:
        return ("SELECT id FROM pipelines WHERE name LIKE :q "
                "ORDER BY id DESC LIMIT :lim",
                {"q": f"%{q}%", "lim": limit})
    return ("SELECT id FROM pipelines ORDER BY id DESC LIMIT :lim",
            {"lim": limit})


def _q_users(q: str, limit: int):
    if q:
        return ("SELECT id FROM users "
                "WHERE deleted_at IS NULL AND enable = 1 "
                "  AND (name LIKE :q OR username LIKE :q OR email LIKE :q) "
                "ORDER BY id ASC LIMIT :lim",
                {"q": f"%{q}%", "lim": limit})
    return ("SELECT id FROM users WHERE deleted_at IS NULL AND enable = 1 "
            "ORDER BY id ASC LIMIT :lim",
            {"lim": limit})


def _q_teams(q: str, limit: int):
    if q:
        return ("SELECT id FROM teams WHERE name LIKE :q "
                "ORDER BY id ASC LIMIT :lim",
                {"q": f"%{q}%", "lim": limit})
    return ("SELECT id FROM teams ORDER BY id ASC LIMIT :lim",
            {"lim": limit})


_SEARCH_QUERIES = {
    "job":       _q_jobs,
    "candidate": _q_candidates,
    "company":   _q_companies,
    "pipeline":  _q_pipelines,
    "user":      _q_users,
    "team":      _q_teams,
}
