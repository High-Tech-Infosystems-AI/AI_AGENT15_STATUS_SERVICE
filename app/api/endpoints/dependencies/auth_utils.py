"""
JWT token validation with Redis-backed caching.

Every REST call used to fire a synchronous requests.post() to the Auth Service,
adding ~200-500ms latency per call. Now we:
1. Use async httpx (non-blocking event loop)
2. Cache validated token → user_info in Redis for 60 seconds
3. Cached responses return instantly (< 5ms)
"""

import json
import logging
import hashlib
from typing import Optional

import httpx
from fastapi import HTTPException, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core import settings

logger = logging.getLogger("app_logger")
security = HTTPBearer()

# Cache TTL — 60s is safe; tokens typically last hours so this is just a
# rate-limiter on auth-service calls. Users logged-out will still feel it
# within 1 minute due to session invalidation in the auth service.
TOKEN_CACHE_TTL_SECONDS = 60


def _token_cache_key(token: str) -> str:
    """Use sha256 of the token as cache key (don't store raw tokens in Redis).

    The `v2:` prefix is a schema version. Bumped when the cached payload
    gains a new required field (most recently: `organization_id`). Old
    `auth:token:*` entries silently fall back to a re-fetch instead of
    returning a stale shape that downstream code can't handle.
    """
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return f"auth:token:v2:{h}"


def _get_redis():
    """Lazy Redis client — reuses the same connection pool as redis_manager."""
    from app.notification_layer import redis_manager
    return redis_manager.get_notification_redis()


async def validate_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Validate JWT via Auth Service with Redis caching."""
    token = credentials.credentials

    # 1. Try Redis cache first
    try:
        r = _get_redis()
        cached = r.get(_token_cache_key(token))
        if cached:
            info = json.loads(cached)
            info["token"] = token  # don't cache the raw token
            return info
    except Exception as e:
        logger.debug("Token cache read failed: %s", e)

    # 2. Cache miss — call Auth Service (async, non-blocking)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{settings.AUTH_SERVICE_URL}",
                params={"token": token},
                headers={"accept": "application/json"},
            )
    except Exception as e:
        logger.error("Auth service unreachable: %s", e)
        raise HTTPException(status_code=401, detail="Authentication service unavailable")

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    token_info = resp.json()
    user_id = token_info.get("user_id")
    role_id = token_info.get("role_id")
    role_name = token_info.get("role_name")

    if not user_id or not role_id or not role_name:
        raise HTTPException(status_code=401, detail="Token missing required user information")

    info = {
        "user_id": user_id,
        "role_id": role_id,
        "role_name": role_name,
        # Tenant context — NULL for super_admin (platform-wide), set for
        # everyone else. Used by tenant-scoped endpoints (e.g. notifications)
        # to derive the caller's org without an extra DB hit.
        "organization_id": token_info.get("organization_id"),
    }

    # 3. Cache for future requests
    try:
        r = _get_redis()
        r.setex(_token_cache_key(token), TOKEN_CACHE_TTL_SECONDS, json.dumps(info))
    except Exception as e:
        logger.debug("Token cache write failed: %s", e)

    info["token"] = token
    return info


def check_admin_access(role_name: str) -> bool:
    return role_name.lower() in ["admin", "super_admin"]


def resolve_effective_org_id(
    user_info: dict, requested_org_id: Optional[int],
) -> Optional[int]:
    """Tenant-scoping rule shared by every admin write endpoint.

    - super_admin: caller chooses via `?organization_id=N` query param
      (None → platform-wide, used only when they want a literal global
      broadcast). Their JWT org is NULL so we trust the query value.
    - tier / admin: caller's JWT org is the source of truth — any
      query / body value is silently ignored. This is the cross-tenant
      escape guard.
    """
    role_name = (user_info.get("role_name") or "").lower()
    if role_name == "super_admin":
        return requested_org_id
    return user_info.get("organization_id")


def check_user_candidate_access(user_id: int, candidate_created_by: int,
                                 candidate_assigned_to: int = None) -> bool:
    return user_id == candidate_created_by or user_id == candidate_assigned_to
