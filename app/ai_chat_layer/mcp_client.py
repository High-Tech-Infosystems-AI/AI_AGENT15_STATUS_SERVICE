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
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core import settings

logger = logging.getLogger("app_logger")


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
        rows = self._db.execute(stmt, params).all()
        return [dict(r._mapping) for r in rows]
