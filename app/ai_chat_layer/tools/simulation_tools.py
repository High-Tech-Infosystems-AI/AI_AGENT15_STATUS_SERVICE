"""What-if simulation tool — pure-python projection over recruitment metrics.

The model sees "what happens if we add 2 more recruiters / extend the
deadline / boost the conversion rate?" and calls this tool with explicit
overrides. Output is a table the agent can summarize plus a chart spec
that `render_adhoc_chart` can consume directly.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")


class WhatIfArgs(BaseModel):
    baseline_throughput_per_recruiter_per_week: float = Field(
        ..., gt=0, description="Current weekly stage moves per recruiter.")
    current_recruiters: int = Field(..., ge=1)
    delta_recruiters: int = Field(default=0,
                                  description="+N adds recruiters, -N removes.")
    weeks: int = Field(default=8, ge=1, le=52)
    conversion_rate: float = Field(default=0.18, gt=0, le=1.0,
                                   description="Joined / total funnel.")
    conversion_uplift: float = Field(default=0.0, ge=-0.5, le=0.5,
                                     description="Additive shift to conversion (e.g. +0.05).")
    label: Optional[str] = None


def _whatif_throughput(ctx: ToolContext, args: WhatIfArgs) -> Dict[str, Any]:
    base_recruiters = max(1, args.current_recruiters)
    new_recruiters = max(0, base_recruiters + args.delta_recruiters)

    base_per_week = args.baseline_throughput_per_recruiter_per_week
    base_conv = max(0.001, args.conversion_rate)
    new_conv = max(0.001, min(0.999, args.conversion_rate + args.conversion_uplift))

    weekly_baseline = base_per_week * base_recruiters
    weekly_new = base_per_week * new_recruiters

    weeks = list(range(1, args.weeks + 1))
    cumulative_baseline_funnel: List[float] = []
    cumulative_new_funnel: List[float] = []
    cumulative_baseline_joined: List[float] = []
    cumulative_new_joined: List[float] = []

    base_funnel = 0.0
    new_funnel = 0.0
    for _ in weeks:
        base_funnel += weekly_baseline
        new_funnel += weekly_new
        cumulative_baseline_funnel.append(round(base_funnel, 1))
        cumulative_new_funnel.append(round(new_funnel, 1))
        cumulative_baseline_joined.append(round(base_funnel * base_conv, 1))
        cumulative_new_joined.append(round(new_funnel * new_conv, 1))

    delta_joined = round(cumulative_new_joined[-1] - cumulative_baseline_joined[-1], 1)
    pct_uplift = (delta_joined / cumulative_baseline_joined[-1] * 100
                  if cumulative_baseline_joined[-1] else 0.0)

    label = args.label or (
        f"+{args.delta_recruiters} recruiters, conversion {args.conversion_rate:+.0%}"
        f"{' '+ format(args.conversion_uplift, '+.0%') if args.conversion_uplift else ''}"
    )

    return {
        "label": label,
        "weeks": weeks,
        "cumulative_baseline_joined": cumulative_baseline_joined,
        "cumulative_new_joined": cumulative_new_joined,
        "delta_joined": delta_joined,
        "percent_uplift": round(pct_uplift, 1),
        "assumptions": {
            "baseline_per_recruiter_per_week": base_per_week,
            "current_recruiters": base_recruiters,
            "scenario_recruiters": new_recruiters,
            "baseline_conversion": base_conv,
            "scenario_conversion": new_conv,
        },
        # Chart spec consumable by render_adhoc_chart
        "chart_spec": {
            "title": f"What-if: {label}",
            "chart_type": "line",
            "x_labels": [f"W{w}" for w in weeks],
            "series": [
                {"name": "Baseline (joined)", "values": cumulative_baseline_joined},
                {"name": "Scenario (joined)", "values": cumulative_new_joined},
            ],
            "y_label": "Cumulative joined",
        },
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
            args = WhatIfArgs(**kwargs)
            out = _whatif_throughput(ctx, args)
            ctx.add_trace("whatif_throughput", kwargs,
                          int((time.monotonic() - start) * 1000), True)
            return out
        except Exception as exc:
            ctx.add_trace("whatif_throughput", kwargs,
                          int((time.monotonic() - start) * 1000), False, str(exc))
            return {"error": str(exc)}

    return [StructuredTool.from_function(
        func=_runner,
        name="whatif_throughput",
        description=("Project hiring throughput under explicit assumptions: change "
                     "recruiter count, conversion rate, or duration. Returns a "
                     "weekly cumulative-joined comparison plus a chart spec that "
                     "render_adhoc_chart can render."),
        args_schema=WhatIfArgs,
    )]
