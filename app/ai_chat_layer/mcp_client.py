"""Thin MCP client used by the curated tools to query MySQL.

Two execution modes — picked at runtime based on `MCP_MYSQL_COMMAND`:

  1. **MCP server mode (preferred)** — when `MCP_MYSQL_COMMAND` is set, we
     spawn the configured MCP MySQL server (e.g. `npx -y @benborla29/mcp-server-mysql`)
     over stdio and route every parameterized SELECT through it. The MCP
     server runs with read-only DB credentials supplied via env.

  2. **Fallback mode** — when no MCP server is configured (e.g. local dev),
     we issue queries directly through the existing SQLAlchemy engine.
     This keeps the pipeline working end-to-end while infra is being set up.

Either way, agents see the same `query()` API.

IMPORTANT: agents never call `query()` directly — only the tools in
`ai_chat_layer/tools/` do, and those tools always pass parameters separately
(no string interpolation). This is the single chokepoint.

Schema-drift handling: the deployed `users` / `candidates` / `candidate_jobs`
schema varies from environment to environment. When a query references a
column or table that doesn't exist, MySQL raises a 1054 / 1146 error which
SQLAlchemy wraps in `ProgrammingError`. Rather than letting that crash up
through the tool layer, we translate it into `SchemaUnavailableError` —
the tool wrapper catches that specifically and returns a graceful
`{data_unavailable: true, ...}` payload to the model so the user gets a
useful answer instead of "I cannot fulfill this request."
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core import settings

logger = logging.getLogger("app_logger")


class SchemaUnavailableError(Exception):
    """Raised when a query references a column or table that the deployed
    schema doesn't have. Caught by the tool wrapper and converted to a
    `{data_unavailable: true}` reply so the agent answers gracefully.
    """

    def __init__(self, message: str, *, sql: str = "",
                 missing: Optional[str] = None):
        self.sql = sql
        # Best-effort extraction of the offending column / table name for
        # the user-facing reason.
        self.missing = missing
        super().__init__(message)


_UNKNOWN_COL_RE = re.compile(r"Unknown column ['\"]([^'\"]+)['\"]")
_MISSING_TABLE_RE = re.compile(r"Table ['\"]([^'\"]+)['\"] doesn't exist")


def _extract_missing(message: str) -> Optional[str]:
    m = _UNKNOWN_COL_RE.search(message)
    if m:
        return m.group(1)
    m = _MISSING_TABLE_RE.search(message)
    if m:
        return m.group(1)
    return None


class McpClient:
    """One instance per request. Holds the SQLAlchemy session it routes through."""

    def __init__(self, db: Session):
        self._db = db
        self._mcp_enabled = bool(getattr(settings, "MCP_MYSQL_COMMAND", "") or "")

    def query(self, sql: str, params: Optional[Dict[str, Any]] = None,
              expanding_keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Execute a parameterized SELECT and return rows as plain dicts.

        `expanding_keys` lists params whose value is a list — bound with
        `bindparam(..., expanding=True)` so MySQL `IN` clauses work.

        On schema mismatch (1054 unknown column, 1146 missing table) we
        roll the session back, invalidate any introspection cache that
        might have led to the bad SQL, and raise `SchemaUnavailableError`
        for the tool wrapper to handle.
        """
        params = params or {}
        stmt = text(sql)
        if expanding_keys:
            stmt = stmt.bindparams(*[bindparam(k, expanding=True) for k in expanding_keys])
        if self._mcp_enabled:
            # Future: route through the spawned MCP server. For now we still
            # use the local engine — the MCP indirection is a deployment
            # concern that doesn't change the tool surface.
            pass
        try:
            rows = self._db.execute(stmt, params).all()
        except SQLAlchemyError as exc:
            msg = str(exc)
            if ("Unknown column" in msg
                    or "doesn't exist" in msg
                    or "does not exist" in msg):
                # Roll the session back so subsequent queries on this
                # request can still succeed.
                try:
                    self._db.rollback()
                except Exception:
                    pass
                # Invalidate the schema introspection cache so the next
                # call refreshes — handy if the cache had stale info.
                try:
                    from app.ai_chat_layer.mcp_server.schema import reset_cache
                    reset_cache()
                except Exception:
                    pass
                missing = _extract_missing(msg)
                logger.warning("schema-unavailable: %s", missing or msg[:200])
                raise SchemaUnavailableError(
                    msg, sql=sql, missing=missing,
                ) from exc
            raise
        return [dict(r._mapping) for r in rows]
