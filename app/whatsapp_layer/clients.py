"""HTTP clients for the internal endpoints the engine consumes.

  - Job service  GET  {JOB_SERVICE_URL}/api/internal/clawback/active        (contract §3.1)
  - Org service  POST {ORG_SERVICE_URL}/organisation/internal/hierarchy/subordinates (§3.2)

Both authenticate with `X-Internal-Token: <INTERNAL_SERVICE_TOKEN>`.
Failures raise ServiceClientError with a message naming the service, so the
scheduler logs say exactly which dependency is down.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("app_logger")

DEFAULT_TIMEOUT = 15.0


class ServiceClientError(Exception):
    def __init__(self, service: str, message: str, status_code: Optional[int] = None):
        super().__init__(f"{service}: {message}")
        self.service = service
        self.message = message
        self.status_code = status_code


def _settings():
    from app.core import settings
    return settings


def _timeout() -> float:
    try:
        return float(getattr(_settings(), "WHATSAPP_HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT)
                     or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _headers() -> Dict[str, str]:
    token = getattr(_settings(), "INTERNAL_SERVICE_TOKEN", "") or ""
    return {"X-Internal-Token": token, "Accept": "application/json"}


def _base(setting_name: str, service: str) -> str:
    base = (getattr(_settings(), setting_name, "") or "").strip().rstrip("/")
    if not base:
        raise ServiceClientError(service, f"{setting_name} is not configured")
    return base


def _call(service: str, method: str, url: str, **kwargs) -> Any:
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=_timeout(), **kwargs)
    except requests.Timeout:
        raise ServiceClientError(service, f"timed out after {_timeout():.0f}s calling {url}")
    except requests.RequestException as exc:
        raise ServiceClientError(service, f"unreachable ({exc.__class__.__name__}) at {url}")
    if resp.status_code in (401, 403):
        raise ServiceClientError(service, "rejected X-Internal-Token (check INTERNAL_SERVICE_TOKEN)",
                                 resp.status_code)
    if resp.status_code >= 400:
        raise ServiceClientError(service, f"HTTP {resp.status_code}: {(resp.text or '')[:300]}",
                                 resp.status_code)
    try:
        return resp.json()
    except ValueError:
        raise ServiceClientError(service, "returned a non-JSON response", resp.status_code)


# ---------------------------------------------------------------------------
# Job service — active clawbacks
# ---------------------------------------------------------------------------
def fetch_active_clawbacks(organization_id: Optional[int] = None,
                           candidate_job_id: Optional[int] = None) -> Dict[str, Any]:
    """Returns {"generated_at", "today", "timezone", "items": [ClawbackItem]}.
    No organization_id means all orgs."""
    service = "Job service"
    url = f"{_base('JOB_SERVICE_URL', service)}/api/internal/clawback/active"
    params: Dict[str, Any] = {}
    if organization_id is not None:
        params["organization_id"] = organization_id
    if candidate_job_id is not None:
        params["candidate_job_id"] = candidate_job_id
    body = _call(service, "GET", url, params=params)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise ServiceClientError(service, "response has no 'items' list")
    return body


def fetch_clawback_item(organization_id: Optional[int],
                        candidate_job_id: int) -> Optional[Dict[str, Any]]:
    body = fetch_active_clawbacks(organization_id, candidate_job_id)
    for item in body["items"]:
        if int(item.get("candidate_job_id") or 0) == int(candidate_job_id):
            return item
    return None


# ---------------------------------------------------------------------------
# Org service — hierarchy subordinates
# ---------------------------------------------------------------------------
def fetch_subordinates(organization_id: int, audience: str, *,
                       user_ids: Optional[List[int]] = None,
                       min_level: Optional[int] = None,
                       max_level: Optional[int] = None,
                       role_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """Returns [{"user_id", "name", "is_team_manager", "designation_level",
    "subordinate_user_ids", "direct_team_user_ids"}]."""
    service = "Org service"
    url = f"{_base('ORG_SERVICE_URL', service)}/organisation/internal/hierarchy/subordinates"
    payload = {
        "organization_id": organization_id,
        "audience": audience,
        "user_ids": user_ids,
        "min_level": min_level,
        "max_level": max_level,
        "role_ids": role_ids,
    }
    body = _call(service, "POST", url, json=payload)
    if not isinstance(body, dict) or not isinstance(body.get("users"), list):
        raise ServiceClientError(service, "response has no 'users' list")
    return body["users"]


def subordinates_of(organization_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """The org-service record for one user (their subtree), or None."""
    users = fetch_subordinates(organization_id, "users", user_ids=[user_id])
    for u in users:
        if int(u.get("user_id") or 0) == int(user_id):
            return u
    return None
