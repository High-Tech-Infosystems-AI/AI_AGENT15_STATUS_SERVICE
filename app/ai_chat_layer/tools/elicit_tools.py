"""Elicitation tool — the agent's way to ask the user a structured
follow-up question (single-select / multi-select / free text / number /
date) instead of a free-form prose question.

When the model decides it needs more information before answering, it
calls `request_elicitation(...)`. The tool emits a chat ref of type
`ai_elicitation` carrying the field spec; the FE renders that ref as an
inline form (radio buttons, dropdown, text inputs, submit button). When
the user submits, the FE POSTs to /ai-chat/elicit/respond, which feeds
the structured answer back into a fresh agent turn.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")

FieldKind = Literal["select", "multiselect", "text", "number", "date", "buttons"]


class ElicitOption(BaseModel):
    """One option for select / multiselect / buttons fields."""
    value: str = Field(..., max_length=120)
    label: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=200)


class ElicitField(BaseModel):
    name: str = Field(..., min_length=1, max_length=64,
                      description="Stable key the FE returns under in the answer dict.")
    label: str = Field(..., min_length=1, max_length=200)
    kind: FieldKind = "text"
    placeholder: Optional[str] = Field(default=None, max_length=200)
    required: bool = True
    options: List[ElicitOption] = Field(default_factory=list,
                                         description="Required for select/multiselect/buttons.")
    default: Optional[str] = None


class ElicitArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200,
                       description="One-line headline for the form.")
    intro: Optional[str] = Field(default=None, max_length=600,
                                  description="Optional helper text shown below the title.")
    fields: List[ElicitField] = Field(..., min_length=1, max_length=8)
    submit_label: str = Field(default="Submit", max_length=40)


def _request_elicitation(ctx: ToolContext, args: ElicitArgs) -> Dict[str, Any]:
    """Emit an inline form ref. The FE renders the form, the user submits,
    and the answer comes back as a fresh /ai-chat/ask call carrying
    `prompt = "[elicit:<id>] {json answer}"`. We use a uuid so the agent
    can correlate the answer to the original question turn."""
    elicit_id = uuid.uuid4().hex[:12]
    spec = {
        "id": elicit_id,
        "title": args.title,
        "intro": args.intro,
        "fields": [f.model_dump() for f in args.fields],
        "submit_label": args.submit_label,
    }
    ref = {
        "type": "ai_elicitation",
        "id": elicit_id,
        "params": spec,
    }
    ctx.add_output_ref(ref)
    return {
        "elicitation_id": elicit_id,
        "rendered": True,
        "spec": spec,
        "note": ("The form is now visible to the user. Stop. Wait for the "
                 "user's submission to arrive as a follow-up message — do "
                 "not call other tools or compose more text in this turn."),
    }


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
            args = ElicitArgs(**kwargs)
            out = _request_elicitation(ctx, args)
            ctx.add_trace("request_elicitation", kwargs,
                          int((time.monotonic() - start) * 1000), True)
            return out
        except Exception as exc:
            ctx.add_trace("request_elicitation", kwargs,
                          int((time.monotonic() - start) * 1000), False, str(exc))
            return {"error": str(exc)}

    return [StructuredTool.from_function(
        func=_runner,
        name="request_elicitation",
        description=(
            "Ask the user a structured follow-up question with form fields "
            "(select / multiselect / text / number / date / buttons). Call "
            "this when you need a clarification you can't safely guess and "
            "the user would benefit from picking from explicit options "
            "instead of typing free text. Each field has a `name` (the key "
            "the answer comes back under), `label`, `kind`, and (for "
            "select/multiselect/buttons) an `options` list. After calling, "
            "STOP — wait for the user's submission as the next user turn."
        ),
        args_schema=ElicitArgs,
    )]
