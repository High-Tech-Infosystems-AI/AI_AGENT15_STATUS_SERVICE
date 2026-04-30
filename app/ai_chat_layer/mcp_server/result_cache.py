"""Redis-backed query-result cache.

Most asks hit the same `(measure, dimensions, filters, scope)` tuple
many times — different turns or different users with the same role on
the same dashboard data. We cache the rows for `AI_QUERY_CACHE_TTL`
seconds (default 5 min) keyed on a hash of the SQL + bound params +
the caller's role-scope fingerprint.

The cache is intentionally per-scope: an admin's result must NOT serve
a recruiter, and vice versa, even for "the same SQL" (the SQL itself
already includes the scope IN clause, but the param list differs by
recruiter, so the hash naturally diverges; the role tag here is belt-
and-suspenders).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from app.ai_chat_layer.access_middleware import CallerScope
from app.core import settings
from app.notification_layer import redis_manager

logger = logging.getLogger("app_logger")

_DEFAULT_TTL = 300


def _ttl() -> int:
    return int(getattr(settings, "AI_QUERY_CACHE_TTL", _DEFAULT_TTL) or _DEFAULT_TTL)


def _scope_fingerprint(scope: CallerScope) -> str:
    """Stable hash representing the caller's data-visibility scope."""
    payload = {
        "admin": bool(scope.unscoped),
        "jobs": sorted(scope.job_ids) if not scope.unscoped else None,
        "cands": sorted(scope.candidate_ids) if not scope.unscoped else None,
        "cos": sorted(scope.company_ids) if not scope.unscoped else None,
    }
    return hashlib.sha256(
        json.dumps(payload, default=str).encode("utf-8")
    ).hexdigest()[:16]


def cache_key(sql: str, params: Dict[str, Any], scope: CallerScope) -> str:
    """Build a deterministic cache key for the (sql, params, scope) tuple."""
    blob = json.dumps(
        {"sql": sql, "params": params, "scope": _scope_fingerprint(scope)},
        sort_keys=True, default=str,
    )
    return "ai:qcache:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def get(key: str) -> Optional[Any]:
    try:
        raw = redis_manager.get_notification_redis().get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("query cache read failed: %s", exc)
        return None


def set(key: str, value: Any, ttl: Optional[int] = None) -> None:
    try:
        redis_manager.get_notification_redis().setex(
            key, ttl or _ttl(),
            json.dumps(value, default=str),
        )
    except Exception as exc:
        logger.warning("query cache write failed: %s", exc)


def invalidate_user(user_id: int) -> None:
    """Best-effort: drop any keys associated with a user. Called when
    SuperAdmin changes someone's job assignments. Cheap because keys are
    namespaced by hash and we don't track them explicitly — instead we
    bump a per-user epoch and include it in the scope fingerprint
    (future enhancement). For now this is a no-op stub."""
    _ = user_id  # suppress unused warning until the epoch story lands.
