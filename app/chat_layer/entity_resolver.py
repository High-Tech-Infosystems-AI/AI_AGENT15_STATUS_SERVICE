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
    if not ids:
        return []
    rows = db.execute(text("""
        SELECT j.id, j.title, j.status, j.deadline,
               j.openings, j.location, j.work_mode,
               c.company_name AS company_name
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
        ]
        if m["deadline"]:
            fields.append({"label": "Deadline", "value": m["deadline"].isoformat()})
        if m["work_mode"]:
            fields.append({"label": "Mode", "value": m["work_mode"]})
        out.append({
            "type": "job",
            "id": m["id"],
            "title": m["title"] or f"Job {m['id']}",
            "subtitle": m["company_name"] or m["location"] or None,
            "status": (m["status"] or "").upper() or None,
            "status_color": _status_color_for(m["status"]),
            "deep_link": f"/edit-jobs/{m['id']}",
            "fields": fields,
        })
    return out


def _resolve_candidates(db: Session, ids: List[int]) -> List[Optional[dict]]:
    if not ids:
        return []
    rows = db.execute(text("""
        SELECT id, candidate_name, candidate_email, candidate_status,
               experience, current_company
          FROM candidates
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
        if m["experience"] is not None:
            fields.append({"label": "Experience", "value": f"{m['experience']} yrs"})
        if m["current_company"]:
            fields.append({"label": "Currently at", "value": m["current_company"]})
        # candidate_status is a TEXT column — keep first ~20 chars for the pill.
        status_raw = (m["candidate_status"] or "").strip()
        status = (status_raw.split("\n", 1)[0][:20]).upper() if status_raw else None
        out.append({
            "type": "candidate",
            "id": m["id"],
            "title": m["candidate_name"] or f"Candidate {m['id']}",
            "subtitle": m["candidate_email"] or None,
            "status": status,
            "status_color": _status_color_for(status_raw),
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
               r.role_name
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
    {"id": "pipeline-funnel",       "title": "Pipeline Funnel",
     "subtitle": "Stage-by-stage candidate count"},
    {"id": "hiring-funnel",         "title": "Hiring Funnel",
     "subtitle": "Joined vs rejected over time"},
    {"id": "daily-trend",           "title": "Daily Trend",
     "subtitle": "Joined / rejected (hourly–yearly)"},
    {"id": "latest-jobs",           "title": "Latest Jobs",
     "subtitle": "Most recent job openings"},
    {"id": "count-jobs",            "title": "Job Count",
     "subtitle": "Aggregate open job count"},
    {"id": "company-jobs-count",    "title": "Jobs by Company",
     "subtitle": "Per-company job distribution"},
    {"id": "count-candidates",      "title": "Candidate Count",
     "subtitle": "Aggregate candidate count"},
    {"id": "user-candidate-share-today", "title": "Recruiter Load Today",
     "subtitle": "Per-recruiter candidate distribution"},
    {"id": "hiring-summary-details", "title": "Hiring Summary",
     "subtitle": "Detailed hiring metrics"},
    {"id": "pipeline-progress-details", "title": "Pipeline Progress",
     "subtitle": "Stage-wise progress breakdown"},
    {"id": "clawback-metrics",      "title": "Clawback Metrics",
     "subtitle": "Clawback rates by recruiter"},
    {"id": "daily-performance",     "title": "Daily Performance",
     "subtitle": "Recruiter daily performance"},
    {"id": "avg-time-stages",       "title": "Avg Time Per Stage",
     "subtitle": "Average duration per pipeline stage"},
    {"id": "pipeline-velocity",     "title": "Pipeline Velocity",
     "subtitle": "Candidates moving per day"},
]
REPORTS_BY_ID = {r["id"]: r for r in REPORTS_CATALOG}


def _resolve_reports(_db: Session, ids: List[str]) -> List[Optional[dict]]:
    out: List[Optional[dict]] = []
    for rid in ids:
        meta = REPORTS_BY_ID.get(rid)
        if not meta:
            out.append(None)
            continue
        out.append({
            "type": "report",
            "id": meta["id"],
            "title": meta["title"],
            "subtitle": meta["subtitle"],
            "status": None,
            "status_color": None,
            "deep_link": f"/dashboard?chart={meta['id']}",
            "fields": [],
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
    input order. Unknown types or missing rows return None at that index."""
    refs = list(refs)
    out: List[Optional[dict]] = [None] * len(refs)
    by_type: dict[str, list[tuple[int, str | int]]] = {}
    for i, ref in enumerate(refs):
        t = (ref or {}).get("type")
        rid = (ref or {}).get("id")
        if t not in _RESOLVERS or rid is None:
            continue
        by_type.setdefault(t, []).append((i, rid))
    for t, items in by_type.items():
        positions = [i for i, _ in items]
        ids = [rid for _, rid in items]
        try:
            cards = _RESOLVERS[t](db, ids)
        except Exception as e:
            logger.exception("entity resolve failed type=%s ids=%s: %s", t, ids, e)
            cards = [None] * len(ids)
        for pos, card in zip(positions, cards):
            out[pos] = card
    return out


# ---------------------------------------------------------------------------
# Picker search — used by the + button modal and inline @-autocomplete.
# Returns up to `limit` results in card-shape.
# ---------------------------------------------------------------------------

def search(db: Session, *, type_: str, q: str, limit: int = 12) -> List[dict]:
    q = (q or "").strip()
    if type_ not in _RESOLVERS:
        return []
    if type_ == "report":
        ql = q.lower()
        if not ql:
            picks = REPORTS_CATALOG[:limit]
        else:
            picks = [r for r in REPORTS_CATALOG
                     if ql in r["title"].lower()
                     or ql in (r.get("subtitle") or "").lower()][:limit]
        return [c for c in _resolve_reports(db, [r["id"] for r in picks]) if c]

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
    if q:
        return ("SELECT id FROM candidates "
                "WHERE candidate_name LIKE :q OR candidate_email LIKE :q "
                "ORDER BY id DESC LIMIT :lim",
                {"q": f"%{q}%", "lim": limit})
    return ("SELECT id FROM candidates ORDER BY id DESC LIMIT :lim",
            {"lim": limit})


def _q_companies(q: str, limit: int):
    if q:
        return ("SELECT id FROM companies "
                "WHERE company_name LIKE :q OR location LIKE :q "
                "ORDER BY id DESC LIMIT :lim",
                {"q": f"%{q}%", "lim": limit})
    return ("SELECT id FROM companies ORDER BY id DESC LIMIT :lim",
            {"lim": limit})


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
