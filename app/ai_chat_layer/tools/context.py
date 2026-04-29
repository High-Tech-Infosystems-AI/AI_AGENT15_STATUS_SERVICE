"""Per-request context passed into every tool invocation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.ai_chat_layer.access_middleware import CallerScope
from app.ai_chat_layer.mcp_client import McpClient


@dataclass
class ToolContext:
    """Bundle of objects that all tools need.

    Built once per `ask` request. Tools should never reach back into the
    request directly — everything they need flows through here.
    """
    db: Session
    user: Dict[str, Any]
    scope: CallerScope
    mcp: McpClient
    refs: List[Dict[str, Any]] = field(default_factory=list)
    # Mutable trace — every tool execution appends to this. The agent's
    # final compose pass and the audit row both read from it.
    trace: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    # Resolved entity cards mentioned by tools. Final reply attaches these
    # as `refs` so the FE can render click-through cards.
    output_refs: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def user_id(self) -> int:
        return int(self.user.get("user_id") or 0)

    def add_trace(self, name: str, args: Dict[str, Any], ms: int, ok: bool,
                  error: Optional[str] = None) -> None:
        # Keep arg snapshot small + drop anything that smells like PII.
        clean: Dict[str, Any] = {}
        for k, v in (args or {}).items():
            if k.lower() in {"password", "token", "secret"}:
                continue
            try:
                if isinstance(v, (str, int, float, bool, type(None))):
                    clean[k] = v
                elif isinstance(v, (list, tuple)):
                    clean[k] = list(v)[:8]
                else:
                    clean[k] = str(v)[:200]
            except Exception:
                clean[k] = "<unserializable>"
        self.trace.append({
            "name": name,
            "args": clean,
            "ms": int(ms),
            "ok": bool(ok),
            "error": error[:200] if error else None,
        })

    def add_artifact(self, kind: str, s3_key: str, url: Optional[str],
                     mime: str, meta: Optional[Dict[str, Any]] = None) -> None:
        self.artifacts.append({
            "kind": kind, "s3_key": s3_key, "url": url, "mime": mime,
            "meta": meta or {},
        })

    def add_output_ref(self, ref: Dict[str, Any]) -> None:
        # Avoid duplicates by (type, id) tuple.
        key = (ref.get("type"), str(ref.get("id")))
        for existing in self.output_refs:
            if (existing.get("type"), str(existing.get("id"))) == key:
                return
        self.output_refs.append(ref)
