"""Records behind a dashboard number (contract §6.11): the dashboard picks which
candidates a number stands for and sends their ids; this filters, searches,
sorts, groups and pages them. Keys match the dashboard's top filters."""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

GROUP_BYS = ("candidate", "company", "team", "recruiter", "job")
DIMENSIONS = ("company", "team", "recruiter", "job")
SORTS = ("days_left", "joined_new", "joined_old", "name")
GROUP_SORTS = ("count", "name")
STATUSES = ("offer_accepted", "in_cooling", "cooling_ended", "completed", "dropped")
NO_TEAM = ("none", "No team")
MAX_PAGE_SIZE = 100

Key = Tuple[str, str]


def keys_of(case: Dict[str, Any], dim: str) -> List[Key]:
    """(key, label) groups a case belongs to; a recruiter can be in several teams."""
    if dim == "company":
        return [(case["company_name"], case["company_name"])]
    if dim == "team":
        return [(str(t["id"]), t["name"]) for t in case.get("teams") or []] or [NO_TEAM]
    if dim == "recruiter":
        rid = case.get("recruiter_user_id")
        return [(str(rid) if rid else "none", case["recruiter_name"])]
    return [(str(case["job_id"]), f"{case['job_title']} · {case['company_name']}")]


def _text(case: Dict[str, Any]) -> str:
    teams = " ".join(t["name"] for t in case.get("teams") or [])
    return " ".join([case["candidate_name"], case.get("candidate_id") or "", case["company_name"],
                     case["job_title"], case["recruiter_name"], teams]).lower()


def matches(case: Dict[str, Any], filters: Dict[str, Sequence[str]],
            statuses: Sequence[str], search: str) -> bool:
    if statuses and case["case_status"] not in statuses:
        return False
    for dim in DIMENSIONS:
        wanted = filters.get(dim) or []
        if wanted and not any(k in wanted for k, _ in keys_of(case, dim)):
            return False
    return not search or search in _text(case)


def _sort_key(sort: str) -> Callable[[Dict[str, Any]], Any]:
    if sort == "name":
        return lambda c: c["candidate_name"].lower()
    if sort in ("joined_new", "joined_old"):
        return lambda c: c.get("joined_at") or c.get("offer_accepted_at") or ""
    return lambda c: (c["days_pending"] is None, c["days_pending"] or 0, c["candidate_name"].lower())


def sort_cases(cases: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    return sorted(cases, key=_sort_key(sort), reverse=sort == "joined_new")


def facets(cases: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Filter options for the selection, each with how many candidates it has."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {d: {} for d in DIMENSIONS}
    for c in cases:
        for dim in DIMENSIONS:
            for key, label in keys_of(c, dim):
                opt = out[dim].setdefault(key, {"key": key, "label": label, "count": 0})
                opt["count"] += 1
    return {d: sorted(v.values(), key=lambda o: o["label"].lower()) for d, v in out.items()}


def status_counts(cases: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for c in cases:
        counts[c["case_status"]] = counts.get(c["case_status"], 0) + 1
    return counts


def group(cases: List[Dict[str, Any]], dim: str, sort: str) -> List[Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for c in cases:
        for key, label in keys_of(c, dim):
            row = rows.setdefault(key, {"key": key, "label": label, "count": 0, "cases": []})
            row["count"] += 1
            row["cases"].append(c)
    out = []
    for row in rows.values():
        cases_in = row.pop("cases")
        out.append({**row, "statuses": status_counts(cases_in),
                    "attention": sum(1 for c in cases_in if c.get("alerts"))})
    if sort == "name":
        return sorted(out, key=lambda r: r["label"].lower())
    return sorted(out, key=lambda r: (-r["count"], r["label"].lower()))


def records(rows: List[Dict[str, Any]], ids: Sequence[int], *, filters: Dict[str, Sequence[str]],
            statuses: Sequence[str], search: Optional[str], group_by: str, sort: str,
            page: int, page_size: int) -> Dict[str, Any]:
    """`rows` are the dashboard's cases and offers the viewer may see; only those
    in `ids` count, so nobody can read outside their own dashboard."""
    wanted = set(int(i) for i in ids)
    base = [r for r in rows if r["candidate_job_id"] in wanted]
    q = (search or "").strip().lower()
    hits = [c for c in base if matches(c, filters, statuses, q)]
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    if group_by == "candidate":
        items: List[Dict[str, Any]] = sort_cases(hits, sort)
    else:
        items = group(hits, group_by, sort)
    pages = max(1, -(-len(items) // page_size))
    page = max(1, min(int(page), pages))
    return {"group_by": group_by, "total": len(items), "page": page, "page_size": page_size,
            "pages": pages, "candidates": len(hits), "selection": len(base),
            "items": items[(page - 1) * page_size: page * page_size],
            "status_counts": status_counts(hits), "facets": facets(base)}
