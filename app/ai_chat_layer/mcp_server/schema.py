"""Schema-aware column resolution for the MCP server layer.

Several customer deployments diverge from the reference schema — e.g.
some have `candidate_jobs.stage`, others a `current_stage`, others a
foreign-key `stage_id`, others nothing at all (stage tracked via the
separate `candidate_status` table). Hard-coding column names breaks the
moment a deployment drifts. This module introspects
`information_schema.columns` once per process and returns the column the
caller should use, plus a few helpers that build SQL fragments for the
common query shapes.

Tools call `candidate_jobs_stage_col(db)`; if it returns `None`, the
deployment doesn't track per-(candidate, job) stage at all, and the
tool should return a graceful "stage data unavailable" payload instead
of crashing.
"""
from __future__ import annotations

import logging
from threading import Lock
from typing import Dict, Optional, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

_TABLE_COLS_CACHE: Dict[str, Set[str]] = {}
_CACHE_LOCK = Lock()

# Column-name aliases for "the stage on candidate_jobs", in priority
# order. The first one that exists wins.
_STAGE_ALIASES = (
    "stage",
    "current_stage",
    "pipeline_stage",
    "stage_name",
    "stage_label",
    "candidate_stage",
)


def _columns_of(db: Session, table: str) -> Set[str]:
    """Return the set of column names (lowercased) for `table`. Cached
    per-process — schema is stable for the life of the service."""
    table_l = table.lower()
    cached = _TABLE_COLS_CACHE.get(table_l)
    if cached is not None:
        return cached
    with _CACHE_LOCK:
        cached = _TABLE_COLS_CACHE.get(table_l)
        if cached is not None:
            return cached
        try:
            rows = db.execute(text("""
                SELECT COLUMN_NAME FROM information_schema.columns
                 WHERE table_schema = DATABASE()
                   AND table_name = :t
            """), {"t": table}).all()
            cols = {(r[0] or "").lower() for r in rows if r and r[0]}
        except Exception as exc:
            logger.warning("schema introspect failed for %s: %s", table, exc)
            cols = set()
        _TABLE_COLS_CACHE[table_l] = cols
        return cols


def candidate_jobs_stage_col(db: Session) -> Optional[str]:
    """Return the column on `candidate_jobs` that holds the pipeline
    stage label, or None if none exists in this deployment.

    Pick the first alias from `_STAGE_ALIASES` that's present, mirroring
    the spirit of how the synthetic-user provisioner picks a password
    column. Returns the actual column name as it appears in the DB
    (preserving original casing isn't important — MySQL is case-
    insensitive on identifiers in queries we issue).
    """
    cols = _columns_of(db, "candidate_jobs")
    if not cols:
        return None
    for alias in _STAGE_ALIASES:
        if alias in cols:
            return alias
    return None


def reset_cache() -> None:
    """Test / migration hook to invalidate the cached schema."""
    with _CACHE_LOCK:
        _TABLE_COLS_CACHE.clear()
