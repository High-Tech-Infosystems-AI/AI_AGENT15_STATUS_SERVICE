"""Template variable catalogue (contract §2.3) and `app_link` paths.

Admins write templates with these named keys ({{candidate_name}}); a key
may only be used in a template whose `purpose` is listed for it.
"""
from __future__ import annotations

from typing import Dict, List, Optional

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
