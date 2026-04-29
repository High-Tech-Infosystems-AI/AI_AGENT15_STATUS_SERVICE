"""Redis-backed conversation memory.

Each user has one rolling buffer keyed by `(user_id, conversation_id)`. We
keep the last N raw turns + a rolling summary of older turns. Memory is
cheap to read/write and TTL'd so abandoned sessions don't pile up.

Stored shape:
    ai:session:{user_id}:{conversation_id} = {
       "summary": "...",
       "turns": [{"role":"user"|"assistant","content":"..."}],
    }
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from app.core import settings
from app.notification_layer import redis_manager

logger = logging.getLogger("app_logger")

MAX_BUFFER_TURNS = 12


def _key(user_id: int, conversation_id: int) -> str:
    return f"ai:session:{user_id}:{conversation_id}"


def _ttl() -> int:
    return int(getattr(settings, "AI_SESSION_TTL_SECONDS", 86400) or 86400)


def load(user_id: int, conversation_id: int) -> Dict:
    try:
        raw = redis_manager.get_notification_redis().get(_key(user_id, conversation_id))
        if not raw:
            return {"summary": "", "turns": []}
        obj = json.loads(raw)
        return {
            "summary": obj.get("summary") or "",
            "turns": obj.get("turns") or [],
        }
    except Exception as exc:
        logger.warning("session load failed: %s", exc)
        return {"summary": "", "turns": []}


def save(user_id: int, conversation_id: int, state: Dict) -> None:
    try:
        redis_manager.get_notification_redis().setex(
            _key(user_id, conversation_id),
            _ttl(),
            json.dumps(state, default=str),
        )
    except Exception as exc:
        logger.warning("session save failed: %s", exc)


def append_turn(user_id: int, conversation_id: int, role: str, content: str) -> Dict:
    """Append a turn, compress oldest into the summary if buffer overflows."""
    state = load(user_id, conversation_id)
    turns: List[Dict] = state.get("turns") or []
    turns.append({"role": role, "content": (content or "")[:6000]})

    if len(turns) > MAX_BUFFER_TURNS:
        # Pop oldest turn and fold its content into the summary line.
        old = turns.pop(0)
        summary = state.get("summary") or ""
        snippet = f"{old.get('role')}: {old.get('content','')[:200]}"
        summary = (summary + "\n" + snippet).strip()
        # Hard cap on summary length to keep prompt cheap.
        if len(summary) > 4000:
            summary = summary[-4000:]
        state["summary"] = summary

    state["turns"] = turns
    save(user_id, conversation_id, state)
    return state


def reset(user_id: int, conversation_id: int) -> None:
    try:
        redis_manager.get_notification_redis().delete(_key(user_id, conversation_id))
    except Exception:
        pass
