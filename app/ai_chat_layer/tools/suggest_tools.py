"""`suggest_followups` — emit interactive follow-up buttons.

The model calls this at the end of a reply with 2-4 short prompt strings
the user might want to ask next. Each suggestion becomes a clickable
chip in the chat; tapping one auto-fires that prompt as the user's next
message. Mirrors the elicitation pattern (server-side ref → FE renderer
→ click → next /ai-chat/ask call).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")


class SuggestionItem(BaseModel):
    label: str = Field(..., min_length=1, max_length=80,
                       description="Short button text shown to the user.")
    prompt: Optional[str] = Field(default=None, max_length=240,
                                  description="The full prompt to send when clicked. Defaults to label.")
    icon: Optional[str] = Field(default=None, max_length=4,
                                 description="Single emoji / icon glyph (optional).")


class SuggestArgs(BaseModel):
    suggestions: List[SuggestionItem] = Field(..., min_length=1, max_length=4)
    headline: Optional[str] = Field(default=None, max_length=80,
                                     description="Optional small label above the buttons (e.g. 'Try next').")


def _suggest_followups(ctx: ToolContext, args: SuggestArgs) -> Dict[str, Any]:
    """Attach a button row to the bot's reply. Each entry has `label`
    (button text) and `prompt` (what gets sent on click)."""
    sid = uuid.uuid4().hex[:12]
    items = []
    for s in args.suggestions:
        items.append({
            "label": s.label,
            "prompt": s.prompt or s.label,
            "icon": s.icon,
        })
    ref = {
        "type": "ai_suggestions",
        "id": sid,
        "params": {
            "headline": args.headline,
            "suggestions": items,
        },
    }
    ctx.add_output_ref(ref)
    return {"rendered": True, "count": len(items)}


def build_tools(ctx: ToolContext) -> List[Any]:
    try:
        from langchain.tools import StructuredTool  # type: ignore
    except ImportError:
        try:
            from langchain_core.tools import StructuredTool  # type: ignore
        except ImportError:
            return []

    def _runner(**kwargs):
        start = time.monotonic()
        try:
            args = SuggestArgs(**kwargs)
            out = _suggest_followups(ctx, args)
            ctx.add_trace("suggest_followups", kwargs,
                          int((time.monotonic() - start) * 1000), True)
            return out
        except Exception as exc:
            ctx.add_trace("suggest_followups", kwargs,
                          int((time.monotonic() - start) * 1000), False, str(exc))
            return {"error": str(exc)}

    return [StructuredTool.from_function(
        func=_runner,
        name="suggest_followups",
        description=(
            "Attach 2-4 follow-up suggestion buttons to the end of your "
            "reply. Each item has a `label` (short button text) and an "
            "optional `prompt` (the full text sent when the user clicks; "
            "defaults to the label). Use this to offer the user clear "
            "next-step options like 'Show me the funnel chart' or "
            "'List the top 5 candidates here' instead of writing those "
            "as plain text suggestions. Optional `icon` is a single "
            "emoji per button."
        ),
        args_schema=SuggestArgs,
    )]
