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
    _v("check_action", "What to do", "Progress", "Weekly check-in call", _REMINDER),
    # Lists (recruiter list + team summary)
    _v("due_count", "Checks due", "Lists", "5",
       sorted(set(_BATCH + _DIGEST), key=PURPOSES.index)),
    _v("overdue_count", "Checks overdue", "Lists", "2", _BATCH),
    _v("candidate_count", "Candidates due today", "Lists", "2", _BATCH),
    _v("part_label", "Count and part", "Lists", "3 candidates",
       sorted(set(_BATCH + _DIGEST), key=PURPOSES.index)),
    _v("candidate_list", "Candidates due today (list)", "Lists",
       "1) Ravi K – Acme – joined 12 Oct, day 14 of 90 – Weekly check-in call – "
       "https://hrmis.example.com/hc/101 · 2) Neha P – Zen Foods – offer accepted 20 Oct, "
       "not joined yet – Call to confirm joining – https://hrmis.example.com/hc/102",
       sorted(set(_BATCH + _DIGEST), key=PURPOSES.index)),
    _v("recruiter_count", "Recruiters in the list", "Lists", "3", _DIGEST),
    # Digest
    _v("scope_label", "Scope", "Digest", "Your team (12 recruiters)", _DIGEST),
    _v("period_label", "Period", "Digest", "today", _DIGEST),
    _v("active_count", "Active clawbacks", "Digest", "40", _DIGEST),
    _v("done_count", "Checks done", "Digest", "18", _DIGEST),
    _v("missed_count", "Checks missed", "Digest", "4", _DIGEST),
    _v("at_risk_count", "At risk", "Digest", "2", _DIGEST),
    _v("ending_soon_count", "Ending within 7 days", "Digest", "5", _DIGEST),
    _v("final_pending_count", "Final validations pending", "Digest", "2", _DIGEST),
    # Manager notes (Today's check-ins page)
    _v("sender_name", "Sent by", "Notes", "Meera Iyer", ["generic"]),
    _v("note", "Message", "Notes", "Please finish today's calls before 5 pm", ["generic"]),
    # Links
    _v("app_link", "Link to the HRMIS", "Links",
       "https://hrmis.example.com/clawback/health-checks", list(PURPOSES)),
]

VARIABLES_BY_KEY: Dict[str, Dict] = {v["key"]: v for v in VARIABLES}

# app_link path per purpose (contract §2.3). `generic` has no fixed target.
APP_LINK_PATHS = {
    "recruiter_reminder": "/clawback/health-checks?check={health_check_id}",
    "recruiter_batch": "/clawback/today",
    "final_validation": "/job-pipeline/{job_id}?tab=clawback",
    "team_digest": "/clawback/today",
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
# candidate_list (contract §6.4): one line per message, split into parts
# ---------------------------------------------------------------------------
# A WhatsApp body is at most 1024 characters with the variables filled in,
# and a variable can't hold line breaks, so a list is one line and long lists
# go out as several messages ("part 1 of 3"). 700 leaves room for the
# template's own text.
CANDIDATE_LIST_MAX_CHARS = 700
CANDIDATE_LIST_SEPARATOR = " · "
GROUP_SEPARATOR = " | "
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


def check_link(base_url: Optional[str], health_check_id: Optional[int]) -> str:
    """Short link to one health check (`/hc/<id>` redirects to the check);
    without a check, the health-check list."""
    base = (base_url or "").rstrip("/")
    if health_check_id:
        return f"{base}/hc/{int(health_check_id)}"
    return f"{base}/clawback/health-checks"


def progress_text(anchor: Optional[str], *, joining_date: Optional[date] = None,
                  day_number: Any = None, cooling_period_days: Any = None,
                  offer_accepted_date: Optional[date] = None,
                  completion_date: Optional[date] = None) -> str:
    """offer_accepted: "offer accepted 20 Oct, not joined yet"
    doj:            "joined 12 Oct, day 14 of 90"
    cooling_end:    "joined 12 Oct, cooling period ended 10 Jan"
    """
    if anchor == "offer_accepted":
        return f"offer accepted {short_date(offer_accepted_date)}, not joined yet"
    joined = f"joined {short_date(joining_date)}"
    if anchor == "cooling_end":
        return f"{joined}, cooling period ended {short_date(completion_date)}"
    return f"{joined}, day {sanitize_text(day_number)} of {sanitize_text(cooling_period_days)}"


def candidate_list_entry(n: int, *, candidate_name: Any, company_name: Any, progress: str,
                         link: str, action: Any = None) -> str:
    """"1) Ravi K – Acme – joined 12 Oct, day 14 of 90 – Weekly check-in call – <link>"
    (`action` is left out of team summaries)."""
    parts = [f"{n}) {sanitize_text(candidate_name)}", sanitize_text(company_name),
             sanitize_text(progress)]
    if action:
        parts.append(sanitize_text(action))
    parts.append(sanitize_text(link))
    return " – ".join(parts)


def _render(groups: List[Tuple[Optional[str], List[str]]]) -> str:
    return GROUP_SEPARATOR.join(
        (f"{head}: " if head else "") + CANDIDATE_LIST_SEPARATOR.join(texts)
        for head, texts in groups)


def split_list(entries: Sequence[Tuple[Optional[str], Optional[str], str]],
               max_chars: int = CANDIDATE_LIST_MAX_CHARS) -> List[Tuple[str, int]]:
    """Pack entries into as few one-line parts as fit `max_chars` each.
    Returns [(part text, number of entries in it)].

    `entries` are (group header, header when continued, text); a None header
    means an ungrouped list. A group cut between parts repeats its
    "continued" header at the start of the next part. An entry too long on
    its own is cut with "…" so it still goes out."""
    parts: List[Tuple[str, int]] = []
    groups: List[Tuple[Optional[str], List[str]]] = []
    count = 0
    current: Optional[Tuple[Optional[str], Optional[str]]] = None
    for head, contd, text in entries:
        text = sanitize_text(text)
        new_group = (head, contd) != current
        if new_group:
            trial = groups + [(head, [text])]
        else:
            trial = groups[:-1] + [(groups[-1][0], groups[-1][1] + [text])]
        if groups and len(_render(trial)) > max_chars:
            parts.append((_render(groups), count))
            groups, count = [(head if new_group else (contd or head), [text])], 0
        else:
            groups = trial
        count += 1
        if len(_render(groups)) > max_chars:   # one entry alone is too long
            label = groups[-1][0]
            room = max_chars - len(_render([(label, [""])])) - 1
            groups = [(label, [text[:max(0, room)].rstrip() + "…"])]
        current = (head, contd)
    if groups:
        parts.append((_render(groups), count))
    return parts


def part_label(count: int, part: int, total_parts: int, noun: str = "candidate") -> str:
    """"3 candidates" / "9 candidates, part 1 of 2"."""
    label = f"{count} {noun}{'' if count == 1 else 's'}"
    return label if total_parts <= 1 else f"{label}, part {part} of {total_parts}"
