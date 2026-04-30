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
    data_tools,
    elicit_tools,
    pdf_tools,
    simulation_tools,
)
from app.ai_chat_layer.tools.context import ToolContext  # noqa: F401


def get_registry(ctx) -> List[Any]:
    """Return all tool definitions bound to the request context."""
    tools: List[Any] = []
    tools.extend(data_tools.build_tools(ctx))
    tools.extend(chart_tools.build_tools(ctx))
    tools.extend(pdf_tools.build_tools(ctx))
    tools.extend(simulation_tools.build_tools(ctx))
    tools.extend(elicit_tools.build_tools(ctx))
    return tools
