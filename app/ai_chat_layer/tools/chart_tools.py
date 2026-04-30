"""Chart tools — the AI's visual output channel.

Two strategies, picked by the model:

  - `render_chart(chart_id, params)` — preferred path when the requested
    chart matches an existing dashboard chart_id (daily-trend, hiring-funnel,
    pipeline-funnel, etc). The frontend renders it interactively via
    ReportSnapshot, exactly like a tagged report card. No image, no S3.

  - `render_adhoc_chart(spec)` — fallback when the data shape doesn't match
    a known dashboard. Generates a matplotlib PNG, stores it in S3, returns
    the signed URL. Used for what-if simulations, custom comparisons, etc.
"""
from __future__ import annotations

import io
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai_chat_layer.s3_helper import upload_ai_artifact
from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")


KNOWN_CHART_IDS = {
    "daily-trend", "count-jobs", "latest-jobs",
    "company-jobs-count", "company-performance",
    "recruiter-efficiency", "recruiter-efficiency-top-performers",
    "top-recruiters", "pipeline-velocity", "avg-time-stages",
    "daily-performance", "platform-metrics",
    "ai-distribution", "hiring-funnel",
    "pipeline-funnel", "pipeline-funnel-graph",
}


class RenderChartArgs(BaseModel):
    chart_id: str = Field(..., description="One of: " + ", ".join(sorted(KNOWN_CHART_IDS)))
    title: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    company_id: Optional[int] = None
    job_id: Optional[int] = None
    user_id: Optional[int] = None


class AdhocSeries(BaseModel):
    name: str
    values: List[float]


class AdhocChartArgs(BaseModel):
    title: str = Field(..., max_length=120)
    chart_type: str = Field(..., pattern="^(bar|line|donut)$")
    x_labels: List[str] = Field(default_factory=list)
    series: List[AdhocSeries] = Field(default_factory=list)
    y_label: Optional[str] = None


def _resolve_filter_names(
    ctx: ToolContext,
    *,
    job_id: Optional[int],
    company_id: Optional[int],
    user_id: Optional[int],
) -> Dict[str, Optional[str]]:
    """Look up display names for the chart's filter ids so the chip strip
    shows e.g. "Job: Customer Service Associate" instead of "Job: #171".
    Returns a dict with optional `job_name` / `company_name` / `user_name`
    keys; missing rows simply omit their name entry."""
    out: Dict[str, Optional[str]] = {}
    try:
        if job_id is not None:
            rows = ctx.mcp.query(
                "SELECT title FROM job_openings WHERE id = :id LIMIT 1",
                {"id": int(job_id)},
            )
            if rows and rows[0].get("title"):
                out["job_name"] = str(rows[0]["title"])
        if company_id is not None:
            rows = ctx.mcp.query(
                "SELECT company_name FROM companies WHERE id = :id LIMIT 1",
                {"id": int(company_id)},
            )
            if rows and rows[0].get("company_name"):
                out["company_name"] = str(rows[0]["company_name"])
        if user_id is not None:
            rows = ctx.mcp.query(
                "SELECT name, username FROM users WHERE id = :id LIMIT 1",
                {"id": int(user_id)},
            )
            if rows:
                m = rows[0]
                out["user_name"] = str(m.get("name") or m.get("username") or "")
    except Exception as exc:  # name lookup is best-effort
        logger.warning("chart filter-name resolve failed: %s", exc)
    return out


def _render_chart(ctx: ToolContext, args: RenderChartArgs) -> Dict[str, Any]:
    if args.chart_id not in KNOWN_CHART_IDS:
        return {"error": f"unknown chart_id: {args.chart_id}",
                "known": sorted(KNOWN_CHART_IDS)}
    params: Dict[str, Any] = {
        "date_from": args.date_from, "date_to": args.date_to,
        "company_id": args.company_id, "job_id": args.job_id,
        "user_id": args.user_id,
    }
    params = {k: v for k, v in params.items() if v is not None}
    # Resolve human-readable names for the chip strip.
    params.update(_resolve_filter_names(
        ctx,
        job_id=args.job_id, company_id=args.company_id, user_id=args.user_id,
    ))
    # Emit a structured report ref the FE renders via ReportSnapshot.
    ref = {
        "type": "report",
        "id": args.chart_id,
        "title": args.title or args.chart_id.replace("-", " ").title(),
        "params": params,
    }
    ctx.add_output_ref(ref)
    return {"rendered": True, "ref": ref, "interactive": True}


def _render_adhoc_chart(ctx: ToolContext, args: AdhocChartArgs) -> Dict[str, Any]:
    # Lazy import — matplotlib is heavy; keep the service start cheap.
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        return {"error": f"matplotlib unavailable: {exc}"}

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    try:
        if args.chart_type == "bar":
            n_series = max(1, len(args.series))
            width = 0.8 / n_series
            x = list(range(len(args.x_labels)))
            for i, s in enumerate(args.series):
                vals = list(s.values) + [0] * max(0, len(x) - len(s.values))
                ax.bar([xi + i * width for xi in x], vals[: len(x)],
                       width=width, label=s.name)
            ax.set_xticks([xi + (n_series - 1) * width / 2 for xi in x])
            ax.set_xticklabels(args.x_labels, rotation=20, ha="right")
        elif args.chart_type == "line":
            for s in args.series:
                vals = list(s.values)
                ax.plot(args.x_labels[: len(vals)], vals, marker="o", label=s.name)
            ax.tick_params(axis="x", rotation=20)
        else:  # donut
            if args.series:
                first = args.series[0]
                ax.pie(first.values, labels=args.x_labels[: len(first.values)],
                       wedgeprops=dict(width=0.4))
                ax.set(aspect="equal")
        if args.y_label and args.chart_type != "donut":
            ax.set_ylabel(args.y_label)
        ax.set_title(args.title)
        if args.series and args.chart_type != "donut":
            ax.legend(loc="best", fontsize=9)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        png_bytes = buf.read()
    finally:
        plt.close(fig)

    # Persist to S3 under ai/{user_id}/...
    key, url = upload_ai_artifact(
        data=png_bytes, mime="image/png",
        user_id=ctx.user_id,
        kind="chart",
        ext="png",
    )
    artifact = {"kind": "chart", "s3_key": key, "url": url, "mime": "image/png"}
    ctx.add_artifact("chart", key, url, "image/png",
                     {"title": args.title, "chart_type": args.chart_type})
    return {"rendered": True, "artifact": artifact}


def build_tools(ctx: ToolContext) -> List[Any]:
    try:
        from langchain.tools import StructuredTool  # type: ignore
    except ImportError:
        try:
            from langchain_core.tools import StructuredTool  # type: ignore
        except ImportError:
            return []

    def _wrap(name, args_schema, fn, description):
        def _runner(**kwargs):
            args = args_schema(**kwargs) if kwargs else args_schema()
            start = time.monotonic()
            try:
                out = fn(ctx, args)
                ctx.add_trace(name, kwargs, int((time.monotonic() - start) * 1000), True)
                return out
            except Exception as exc:
                ctx.add_trace(name, kwargs, int((time.monotonic() - start) * 1000),
                              False, str(exc))
                logger.exception("tool %s failed", name)
                return {"error": str(exc)}

        return StructuredTool.from_function(
            func=_runner, name=name, description=description, args_schema=args_schema,
        )

    return [
        _wrap("render_chart", RenderChartArgs, _render_chart,
              ("Embed an existing dashboard chart inline in the chat reply. "
               "MUST be called whenever the user asks for a chart, funnel, "
               "graph, plot, trend, distribution, or visualization that maps "
               "to a known chart_id (pipeline-funnel, hiring-funnel, "
               "daily-trend, daily-performance, avg-time-stages, "
               "pipeline-velocity, count-jobs, company-jobs-count, "
               "company-performance, recruiter-efficiency, top-recruiters, "
               "platform-metrics, ai-distribution). Pass job_id / "
               "company_id / user_id / date_from / date_to as applicable. "
               "Do NOT describe the chart in text instead of calling this.")),
        _wrap("render_adhoc_chart", AdhocChartArgs, _render_adhoc_chart,
              ("Render an ad-hoc chart (bar/line/donut) from explicit data "
               "you already have. Stored as PNG in S3. Use only when no "
               "dashboard chart_id matches the request shape.")),
    ]
