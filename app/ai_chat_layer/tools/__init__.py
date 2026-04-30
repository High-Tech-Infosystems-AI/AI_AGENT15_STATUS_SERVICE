"""Tool registry for the AI agent.

A "tool" is a typed function the LangGraph agent can call. Each tool has:
  - a name (string the model emits)
  - a Pydantic args schema
  - an implementation that takes `(ctx, **args)` and returns serializable JSON

The registry is built in two layers:
  - `data_tools` — read-only data access (always go through access middleware)
  - `chart_tools`, `pdf_tools`, `simulation_tools` — derived outputs

Use `get_registry()` to get the list of `StructuredTool` objects to bind
to a LangChain Gemini chat model.
"""
from __future__ import annotations

from typing import Any, List

from app.ai_chat_layer.tools import (
    chart_tools,
    comparison_tools,
    data_tools,
    export_tools,
    pdf_tools,
    schedule_tools,
    semantic_tools,
    simulation_tools,
    suggest_tools,
)
from app.ai_chat_layer.tools.context import ToolContext  # noqa: F401


def get_registry(ctx) -> List[Any]:
    """Return all tool definitions bound to the request context.

    The model-facing tool surface is now driven by:
      * `semantic_tools` — `query_data` (the workhorse for analytics),
        `describe_schema`, `list_measures_dimensions`. These replace
        most of the per-question tools that used to live in
        `data_tools` and let the model self-discover what's askable.
      * `chart_tools`, `pdf_tools`, `export_tools` (CSV / markdown +
        artifact registry), `schedule_tools` (recurring reports +
        audit search), `simulation_tools`, `suggest_tools` —
        action / output tools (not queries).
      * `data_tools` — kept for record-fetching (job_detail,
        candidate_detail, search_entities) and as a transitional
        fallback while the semantic layer beds in.

    Elicitation is NOT a model-callable tool. It originates from inside
    data tools (the MCP server layer) when a tool detects an ambiguous
    arg and is forwarded to the chat as an `ai_elicitation` ref by the
    tool wrappers.
    """
    tools: List[Any] = []
    tools.extend(semantic_tools.build_tools(ctx))
    tools.extend(data_tools.build_tools(ctx))
    tools.extend(comparison_tools.build_tools(ctx))
    tools.extend(chart_tools.build_tools(ctx))
    tools.extend(pdf_tools.build_tools(ctx))
    tools.extend(export_tools.build_tools(ctx))
    tools.extend(schedule_tools.build_tools(ctx))
    tools.extend(simulation_tools.build_tools(ctx))
    tools.extend(suggest_tools.build_tools(ctx))
    return tools
