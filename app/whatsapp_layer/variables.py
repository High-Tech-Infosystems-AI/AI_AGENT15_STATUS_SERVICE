"""Template variable catalogue (contract §2.3) and `app_link` paths.

Admins write templates with these named keys ({{candidate_name}}); a key
may only be used in a template whose `purpose` is listed for it.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

PURPOSES = ["recruiter_reminder", "recruiter_batch", "final_validation",
            "team_digest", "generic"]

_REMINDER = ["recruiter_reminder", "final_validation", "generic"]
_BATCH = ["recruiter_batch", "generic"]
_DIGEST = ["team_digest", "generic"]


def _v(key: str, label: str, group: str, sample: str, purposes: List[str]) -> Dict:
    return {"key": key, "label": label, "group": group, "sample": sample,
            "purposes": purposes}


VARIABLES: List[Dict] = [
    # Recruiter / recipient
    _v("recruiter_name", "Recruiter name", "Recruiter / recipient", "Asha R",
       sorted(set(_REMINDER + _BATCH), key=PURPOSES.index)),
    _v("recipient_name", "Recipient name", "Recruiter / recipient", "Meera S", _DIGEST),
    # Candidate / job
    _v("candidate_name", "Candidate name", "Candidate / job", "Ravi K", _REMINDER),
    _v("job_title", "Job title", "Candidate / job", "Sales Executive", _REMINDER),
    _v("company_name", "Company name", "Candidate / job", "Acme", _REMINDER),
    _v("offer_accepted_date", "Offer accepted date", "Candidate / job", "2026-09-20", _REMINDER),
    _v("joining_date", "Joining date (DOJ)", "Candidate / job", "2026-10-12", _REMINDER),
    _v("clawback_completion_date", "Clawback completion date", "Candidate / job",
       "2027-01-10", _REMINDER),
    # Progress
    _v("day_number", "Day number", "Progress", "14", _REMINDER),
    _v("cooling_period_days", "Cooling period (days)", "Progress", "90", _REMINDER),
    _v("stage_label", "Stage", "Progress", "Days 8–30", _REMINDER),
    _v("days_pending", "Days pending", "Progress", "76", _REMINDER),
    _v("progress_percent", "Progress %", "Progress", "15", _REMINDER),
    # Batch
    _v("due_count", "Checks due", "Batch", "5",
       sorted(set(_BATCH + _DIGEST), key=PURPOSES.index)),
    _v("overdue_count", "Checks overdue", "Batch", "2", _BATCH),
    _v("candidate_count", "Candidates due today", "Batch", "2", _BATCH),
    _v("candidate_list", "Candidates due today (list)", "Batch",
       "1) Ravi K – Sales Executive, Acme (day 14 of 90) · "
       "2) Neha P – Store Manager, Acme (offer accepted 20 Oct)", _BATCH),
    # Digest
    _v("scope_label", "Scope", "Digest", "Your team (12 recruiters)", _DIGEST),
    _v("period_label", "Period", "Digest", "today", _DIGEST),
    _v("active_count", "Active clawbacks", "Digest", "40", _DIGEST),
    _v("done_count", "Checks done", "Digest", "18", _DIGEST),
    _v("missed_count", "Checks missed", "Digest", "4", _DIGEST),
    _v("at_risk_count", "At risk", "Digest", "2", _DIGEST),
    _v("ending_soon_count", "Ending within 7 days", "Digest", "5", _DIGEST),
    _v("final_pending_count", "Final validations pending", "Digest", "2", _DIGEST),
    # Links
    _v("app_link", "Link to the HRMIS", "Links",
       "https://hrmis.example.com/clawback/health-checks", list(PURPOSES)),
]

VARIABLES_BY_KEY: Dict[str, Dict] = {v["key"]: v for v in VARIABLES}

# app_link path per purpose (contract §2.3). `generic` has no fixed target.
APP_LINK_PATHS = {
    "recruiter_reminder": "/clawback/health-checks?check={health_check_id}",
    "recruiter_batch": "/clawback/health-checks",
    "final_validation": "/job-pipeline/{job_id}?tab=clawback",
    "team_digest": "/clawback/team-health",
}


def allowed_keys(purpose: str) -> List[str]:
    return [v["key"] for v in VARIABLES if purpose in v["purposes"]]


def sample_for(key: str) -> Optional[str]:
    v = VARIABLES_BY_KEY.get(key)
    return v["sample"] if v else None


def app_link(base_url: Optional[str], purpose: str, *,
             health_check_id: Optional[int] = None,
             job_id: Optional[int] = None) -> str:
    """Build `app_link` = settings.app_base_url + the purpose's path.

    recruiter_reminder without a health check (e.g. send-now with no check
    for today) falls back to the health-check list."""
    base = (base_url or "").rstrip("/")
    if purpose == "recruiter_reminder" and not health_check_id:
        path = "/clawback/health-checks"
    elif purpose == "final_validation" and not job_id:
        path = "/clawback/health-checks"
    else:
        path = APP_LINK_PATHS.get(purpose, "/clawback/health-checks")
        path = path.format(health_check_id=health_check_id, job_id=job_id)
    return f"{base}{path}"


# ---------------------------------------------------------------------------
# candidate_list (contract §6.1)
# ---------------------------------------------------------------------------
CANDIDATE_LIST_MAX_ENTRIES = 8
CANDIDATE_LIST_MAX_CHARS = 600
CANDIDATE_LIST_SEPARATOR = " · "
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WS_RE = re.compile(r"\s+")


def sanitize_text(value: Any) -> str:
    """One clean line: control characters (incl. \\n, \\t) become spaces or go,
    whitespace collapses to single spaces — so never 4+ consecutive spaces
    (Meta rejects those in variables). Empty → "-"."""
    s = "" if value is None else str(value)
    s = "".join(" " if ch.isspace() else ("" if unicodedata.category(ch) == "Cc" else ch)
                for ch in s)
    s = _WS_RE.sub(" ", s).strip()
    return s or "-"


def short_date(value: Optional[date]) -> str:
    """`D Mon`, e.g. "20 Oct" (locale independent). None → "-"."""
    if value is None:
        return "-"
    return f"{value.day} {_MONTHS[value.month - 1]}"


def candidate_list_entry(n: int, *, candidate_name: Any, job_title: Any, company_name: Any,
                         pre_joining: bool = False, day_number: Any = None,
                         cooling_period_days: Any = None,
                         offer_accepted_date: Optional[date] = None) -> str:
    """doj / cooling: "1) Ravi K – Sales Executive, Acme (day 14 of 90)"
    pre-joining:  "1) Ravi K – Sales Executive, Acme (offer accepted 20 Oct)."""
    head = (f"{n}) {sanitize_text(candidate_name)} – {sanitize_text(job_title)}, "
            f"{sanitize_text(company_name)}")
    if pre_joining:
        return f"{head} (offer accepted {short_date(offer_accepted_date)})"
    return f"{head} (day {sanitize_text(day_number)} of {sanitize_text(cooling_period_days)})"


def _join_entries(shown: Sequence[str], more: int) -> str:
    value = CANDIDATE_LIST_SEPARATOR.join(shown)
    if more > 0:
        value = (value + CANDIDATE_LIST_SEPARATOR if value else "") + f"+{more} more"
    return value


def format_candidate_list(entries: Sequence[str],
                          max_entries: int = CANDIDATE_LIST_MAX_ENTRIES,
                          max_chars: int = CANDIDATE_LIST_MAX_CHARS) -> Tuple[str, int]:
    """Join pre-formatted entries with " · ": at most `max_entries`, then
    " · +k more"; the whole value is capped at `max_chars` by dropping entries
    from the end (k adjusted). Returns (value, number of entries shown).

    A single entry longer than the cap on its own is cut with "…" so the
    message still names someone."""
    entries = [sanitize_text(e) for e in entries]
    total = len(entries)
    shown = list(entries[:max_entries])
    value = _join_entries(shown, total - len(shown))
    while len(value) > max_chars and len(shown) > 1:
        shown.pop()
        value = _join_entries(shown, total - len(shown))
    if len(value) > max_chars and shown:
        tail = _join_entries([], total - 1)
        room = max_chars - len(tail) - (len(CANDIDATE_LIST_SEPARATOR) if tail else 0) - 1
        shown = [shown[0][:max(0, room)].rstrip() + "…"]
        value = _join_entries(shown, total - 1)
    return value, len(shown)
