"""
Shared tenant-auth helper — drop-in template for every backend service.

Copy this file into each service as `app/dependencies/tenant_auth.py`
(or `app/dependencies/auth.py` if the service has no existing auth
module). The contract it enforces is documented in:

  AI_AGENT11_RBAC_Service/docs/TENANT_SEGREGATION_PLAN.md §1
  AI_AGENT11_RBAC_Service/docs/RBAC_INTEGRATION.md

Two configuration constants the service must define:

  AUTH_SERVICE_URL = "<login>/auth/ats/verify-token"
  RBAC_SERVICE_URL = "<rbac>/rbac"

Or set the env vars `AUTH_SERVICE_URL` and `RBAC_SERVICE_URL` and the
template reads from os.getenv() at import time.

Performance note (added):
  `get_auth_ctx` previously made 1-2 blocking HTTP calls (10s timeout each)
  to Login/RBAC on EVERY request, uncached. That was the dominant per-request
  latency across all services. This version adds a short-TTL, in-process,
  thread-safe cache keyed by the (hashed) bearer token, and a tighter HTTP
  timeout. Defaults:
    AUTH_CTX_CACHE_TTL  = 30   (seconds; set 0 to disable caching)
    AUTH_CTX_CACHE_MAX  = 10000 (max cached tokens before eviction)
    AUTH_HTTP_TIMEOUT   = 6     (seconds; requests timeout)
  Trade-off: a token revoked/role-changed mid-window is honoured for up to
  TTL seconds. 30s is the standard compromise; lower it if you need tighter
  revocation. The cache is per-process, so it degrades gracefully across
  multiple workers/pods (each just refreshes independently).

Usage in a route:

    from app.dependencies.tenant_auth import (
        AuthCtx, get_auth_ctx, resolve_read_org, resolve_write_org,
    )

    @router.get("/jobs")
    def list_jobs(
        organization_id: Optional[int] = Query(None),
        ctx: AuthCtx = Depends(get_auth_ctx),
        db: Session = Depends(get_db),
    ):
        ctx.require("jobs.read")                              # WHAT
        org_filter = resolve_read_org(organization_id, ctx)    # WHICH
        q = db.query(JobOpening)
        if org_filter is not None:
            q = q.filter(JobOpening.organization_id == org_filter)
        return q.all()
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Set

import requests
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

# Single canonical endpoint: Login's /user-details returns identity + tenant
# + role tier + permission list in one shot. Falls back to AUTH+RBAC pair
# if USER_DETAILS_URL isn't configured (back-compat with older deployments).
USER_DETAILS_URL = os.getenv("USER_DETAILS_URL", "")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "")
RBAC_SERVICE_URL = os.getenv("RBAC_SERVICE_URL", "").rstrip("/")

# ---- Auth perf tuning (env-overridable) -----------------------------------
try:
    _AUTH_TIMEOUT = float(os.getenv("AUTH_HTTP_TIMEOUT", "6"))
except ValueError:
    _AUTH_TIMEOUT = 6.0
try:
    _CACHE_TTL = int(os.getenv("AUTH_CTX_CACHE_TTL", "30"))
except ValueError:
    _CACHE_TTL = 30
try:
    _CACHE_MAX = int(os.getenv("AUTH_CTX_CACHE_MAX", "10000"))
except ValueError:
    _CACHE_MAX = 10000

# token-hash -> (expires_at_monotonic, auth_ctx_kwargs)
_auth_cache: Dict[str, tuple] = {}
_auth_cache_lock = threading.Lock()


def _cache_key(token: str) -> str:
    """Hash the token so raw JWTs aren't held as dict keys."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    if _CACHE_TTL <= 0:
        return None
    now = time.monotonic()
    with _auth_cache_lock:
        item = _auth_cache.get(key)
        if not item:
            return None
        expires_at, data = item
        if expires_at < now:
            _auth_cache.pop(key, None)
            return None
        return data


def _cache_put(key: str, data: Dict[str, Any]) -> None:
    if _CACHE_TTL <= 0:
        return
    now = time.monotonic()
    with _auth_cache_lock:
        if len(_auth_cache) >= _CACHE_MAX:
            # Drop expired entries first; if still full, clear (cheap + rare).
            expired = [k for k, (exp, _) in _auth_cache.items() if exp < now]
            for k in expired:
                _auth_cache.pop(k, None)
            if len(_auth_cache) >= _CACHE_MAX:
                _auth_cache.clear()
        _auth_cache[key] = (now + _CACHE_TTL, data)


def invalidate_auth_cache(token: Optional[str] = None) -> None:
    """Drop a single token (e.g. on logout) or the whole cache."""
    with _auth_cache_lock:
        if token is None:
            _auth_cache.clear()
        else:
            _auth_cache.pop(_cache_key(token), None)


# ---------------------------------------------------------------------
# Auth context
# ---------------------------------------------------------------------
class AuthCtx:
    """The resolved caller — identity + tenant + permissions in one object.

    Built once per request by `get_auth_ctx`. Inject as a FastAPI dependency
    and use it from the route handler.
    """

    __slots__ = (
        "user_id", "organization_id", "role_id", "role_name", "role_type",
        "permissions", "is_super_admin", "is_admin", "_token",
    )

    def __init__(
        self,
        user_id: int,
        organization_id: Optional[int],
        role_id: Optional[int],
        role_name: Optional[str],
        role_type: str,
        permissions: Set[str],
        is_super_admin: bool,
        is_admin: bool,
        token: str,
    ):
        self.user_id = user_id
        self.organization_id = organization_id
        self.role_id = role_id
        self.role_name = role_name
        self.role_type = role_type
        # Copy the set so request-local code can't mutate the cached value.
        self.permissions = set(permissions or [])
        self.is_super_admin = is_super_admin
        self.is_admin = is_admin
        self._token = token

    @property
    def is_admin_tier(self) -> bool:
        """True for super_admin OR admin — both have implicit-all on perms."""
        return self.is_super_admin or self.is_admin

    def can(self, key: str) -> bool:
        if self.is_admin_tier:
            return True
        return key in self.permissions

    def can_any(self, keys) -> bool:
        if self.is_admin_tier:
            return True
        return any(k in self.permissions for k in keys)

    def can_all(self, keys) -> bool:
        if self.is_admin_tier:
            return True
        return all(k in self.permissions for k in keys)

    def require(self, key: str) -> None:
        """Raise 403 if the caller doesn't hold the permission key.

        Bypassed for admin + super_admin tiers — they always pass.
        """
        if not self.can(key):
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: {key}",
            )

    def require_any(self, keys) -> None:
        if not self.can_any(keys):
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission (any of): {sorted(keys)}",
            )

    def require_admin_tier(self) -> None:
        """Allow only super_admin / admin. Used for settings-only endpoints
        that don't map to a single permission key (notifications.send,
        chatbot.configure, etc.)."""
        if not self.is_admin_tier:
            raise HTTPException(
                status_code=403,
                detail="Admin or Super Admin only",
            )


# ---------------------------------------------------------------------
# Network resolution (uncached) — returns AuthCtx kwargs minus the token
# ---------------------------------------------------------------------
def _resolve_auth_data(token: str) -> Dict[str, Any]:
    """Resolve identity + tenant + permissions for a token via the auth
    service(s). Returns the kwargs used to build an AuthCtx (without `token`).

    Preferred path: single call to Login's `/user-details`.
    Fallback path: legacy `verify-token` + RBAC `/me/permissions`.
    """
    # ---- Preferred: single call ----------------------------------------
    if USER_DETAILS_URL:
        try:
            # Send the JWT BOTH ways:
            #   - as `?token=` (Login's /user-details consumes the query param)
            #   - as `Authorization: Bearer …` (some gateways/ingresses require
            #     it on GET requests; without it they 401 before reaching the
            #     Login code, even when the query param is valid).
            r = requests.get(
                USER_DETAILS_URL,
                params={"token": token},
                headers={
                    "accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=_AUTH_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("User-details service unreachable: %s", exc)
            raise HTTPException(503, "Authentication service unavailable")
        if r.status_code != 200:
            logger.warning(
                "User-details returned %s for token sub-prefix=%s — "
                "check that the JWT is unexpired AND the gateway accepts the "
                "Authorization header. Body: %s",
                r.status_code, token[:25] + "...", r.text[:200],
            )
            raise HTTPException(401, "Invalid or expired token")
        body = r.json()
        user_id = body.get("user_id")
        if not user_id:
            raise HTTPException(401, "Invalid token response")
        role_block = body.get("role") or {}
        return dict(
            user_id=user_id,
            organization_id=body.get("organization_id"),
            role_id=body.get("role_id"),
            role_name=body.get("role_name"),
            role_type=(role_block.get("role_type") or "user"),
            permissions=set(body.get("permissions") or []),
            is_super_admin=bool(body.get("is_super_admin")),
            is_admin=bool(body.get("is_admin")),
        )

    # ---- Fallback: legacy two-call flow --------------------------------
    if not AUTH_SERVICE_URL or not RBAC_SERVICE_URL:
        raise HTTPException(
            500,
            "USER_DETAILS_URL (preferred) or AUTH_SERVICE_URL + "
            "RBAC_SERVICE_URL (legacy) env vars must be configured",
        )

    try:
        r = requests.post(
            AUTH_SERVICE_URL,
            params={"token": token},
            headers={"accept": "application/json"},
            timeout=_AUTH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Auth service unreachable: %s", exc)
        raise HTTPException(503, "Authentication service unavailable")
    if r.status_code != 200:
        raise HTTPException(401, "Invalid or expired token")
    base = r.json()
    user_id = base.get("user_id")
    if not user_id:
        raise HTTPException(401, "Invalid token response")

    try:
        p = requests.get(
            f"{RBAC_SERVICE_URL}/me/permissions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_AUTH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("RBAC service unreachable: %s", exc)
        raise HTTPException(503, "Permission service unavailable")
    if p.status_code != 200:
        raise HTTPException(503, "Permission service error")
    perm = p.json()
    role_block = perm.get("role") or {}

    return dict(
        user_id=user_id,
        organization_id=base.get("organization_id"),
        role_id=base.get("role_id"),
        role_name=base.get("role_name"),
        role_type=(role_block.get("role_type") or "user"),
        permissions=set(perm.get("permissions") or []),
        is_super_admin=bool(perm.get("is_super_admin")),
        is_admin=bool(perm.get("is_admin")),
    )


# ---------------------------------------------------------------------
# Dependency: build AuthCtx
# ---------------------------------------------------------------------
def get_auth_ctx(
    authorization: str = Header(..., alias="Authorization"),
) -> AuthCtx:
    """Resolve the caller from the bearer token.

    Result is cached per-token for `AUTH_CTX_CACHE_TTL` seconds (in-process),
    so the upstream auth HTTP call is skipped on the hot path. On a cache miss
    it resolves via `_resolve_auth_data` (single `/user-details` call, or the
    legacy verify-token + /me/permissions pair).
    """
    token = (authorization or "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(401, "Missing bearer token")

    key = _cache_key(token)
    data = _cache_get(key)
    if data is None:
        data = _resolve_auth_data(token)
        _cache_put(key, data)

    return AuthCtx(token=token, **data)


# ---------------------------------------------------------------------
# Org-scope resolvers
# ---------------------------------------------------------------------
def resolve_read_org(
    requested_org_id: Optional[int],
    ctx: AuthCtx,
) -> Optional[int]:
    """Decide the org filter for a list / read.

    - Super Admin → honour the requested filter (None means "all orgs").
    - Admin / User → forced to own org; client-supplied value is ignored.
    - Misconfigured caller (not super_admin AND no org) → 403.
    """
    if ctx.is_super_admin:
        return requested_org_id
    if ctx.organization_id is None:
        raise HTTPException(403, "User is not attached to an organisation")
    return ctx.organization_id


def resolve_write_org(
    target_org_id: Optional[int],
    ctx: AuthCtx,
) -> int:
    """Decide the org id for a create / update.

    - Super Admin → must explicitly provide; rejected if missing.
    - Admin / User → forced to own org; mismatched client value is rejected.
    """
    if ctx.is_super_admin:
        if target_org_id is None:
            raise HTTPException(
                400, "organization_id is required when acting as Super Admin",
            )
        return target_org_id
    if ctx.organization_id is None:
        raise HTTPException(403, "User is not attached to an organisation")
    if target_org_id is not None and target_org_id != ctx.organization_id:
        raise HTTPException(403, "Cannot act on a different organisation")
    return ctx.organization_id


def assert_row_in_scope(row_org_id: Optional[int], ctx: AuthCtx) -> None:
    """Raise 404 if a row's org_id doesn't match the caller's scope.

    Use this for path-scoped reads (`GET /resource/{id}`) so cross-tenant
    enumeration returns 404 rather than 403 (avoids revealing existence).
    """
    if ctx.is_super_admin:
        return
    if row_org_id is None:
        # Unscoped row + non-super-admin caller is suspicious. Treat as 404.
        raise HTTPException(404, "Not found")
    if row_org_id != ctx.organization_id:
        raise HTTPException(404, "Not found")
