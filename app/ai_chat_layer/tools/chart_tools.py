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

from app.ai_chat_layer.s3_helper import register_ai_artifact, upload_ai_artifact
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
    chart_type: str = Field(
        ...,
        pattern="^(bar|line|donut|stacked_bar|histogram|heatmap|scatter)$",
        description=(
            "bar / line / donut: classic. stacked_bar: same x-axis, "
            "stacks `series` on top of each other. histogram: pass "
            "raw values in series[0].values; `bins` controls bucket "
            "count. heatmap: `matrix` is a 2-D list (rows = y_labels, "
            "cells = values), `x_labels` and `y_labels` label axes. "
            "scatter: each `series` entry must have equal-length "
            "x_values + values (we pair them index-wise; pass empty "
            "x_labels)."
        ),
    )
    x_labels: List[str] = Field(default_factory=list)
    series: List[AdhocSeries] = Field(default_factory=list)
    y_label: Optional[str] = None
    # Extra args for the new chart types — unused for bar/line/donut.
    bins: Optional[int] = Field(
        default=20, ge=2, le=100,
        description="Histogram bin count (only used when chart_type='histogram').",
    )
    y_labels: List[str] = Field(
        default_factory=list,
        description="Heatmap row labels (only used when chart_type='heatmap').",
    )
    matrix: List[List[float]] = Field(
        default_factory=list,
        description="Heatmap 2-D values (rows = y_labels, cols = x_labels).",
    )
    x_values: List[List[float]] = Field(
        default_factory=list,
        description=(
            "Scatter x-coordinates per series (parallel to series[].values). "
            "Pass one list per series, equal length to that series' values."
        ),
    )


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
        elif args.chart_type == "stacked_bar":
            x = list(range(len(args.x_labels)))
            bottoms = [0.0] * len(x)
            for s in args.series:
                vals = list(s.values) + [0] * max(0, len(x) - len(s.values))
                vals = vals[: len(x)]
                ax.bar(x, vals, bottom=bottoms, label=s.name)
                bottoms = [b + v for b, v in zip(bottoms, vals)]
            ax.set_xticks(x)
            ax.set_xticklabels(args.x_labels, rotation=20, ha="right")
        elif args.chart_type == "line":
            for s in args.series:
                vals = list(s.values)
                ax.plot(args.x_labels[: len(vals)], vals, marker="o", label=s.name)
            ax.tick_params(axis="x", rotation=20)
        elif args.chart_type == "histogram":
            if args.series:
                first = args.series[0]
                ax.hist(first.values, bins=args.bins or 20,
                        edgecolor="white", linewidth=0.5)
            ax.set_xlabel(args.y_label or "Value")
            ax.set_ylabel("Frequency")
        elif args.chart_type == "heatmap":
            if args.matrix:
                im = ax.imshow(args.matrix, aspect="auto", cmap="viridis")
                fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
                if args.x_labels:
                    ax.set_xticks(range(len(args.x_labels)))
                    ax.set_xticklabels(args.x_labels, rotation=20, ha="right")
                if args.y_labels:
                    ax.set_yticks(range(len(args.y_labels)))
                    ax.set_yticklabels(args.y_labels)
        elif args.chart_type == "scatter":
            for i, s in enumerate(args.series):
                xs = (args.x_values[i] if i < len(args.x_values)
                      else list(range(len(s.values))))
                xs = list(xs)[: len(s.values)]
                ax.scatter(xs, s.values, label=s.name, alpha=0.7)
            if args.y_label:
                ax.set_ylabel(args.y_label)
        else:  # donut
            if args.series:
                first = args.series[0]
                ax.pie(first.values, labels=args.x_labels[: len(first.values)],
                       wedgeprops=dict(width=0.4))
                ax.set(aspect="equal")
        if args.y_label and args.chart_type not in ("donut", "histogram", "scatter"):
            ax.set_ylabel(args.y_label)
        ax.set_title(args.title)
        if (args.series and args.chart_type not in ("donut", "histogram", "heatmap")):
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
    artifact_id = register_ai_artifact(
        db=ctx.db, user_id=ctx.user_id, kind="chart",
        s3_key=key, mime="image/png",
        file_name=f"{args.title[:60].replace(' ', '_')}.png",
        title=args.title,
        meta={"chart_type": args.chart_type},
    )
    artifact = {"kind": "chart", "s3_key": key, "url": url,
                "mime": "image/png", "artifact_id": artifact_id}
    ctx.add_artifact("chart", key, url, "image/png",
                     {"title": args.title, "chart_type": args.chart_type,
                      "artifact_id": artifact_id})
    return {"rendered": True, "artifact": artifact}


class ChartFromDataArgs(BaseModel):
    title: str = Field(..., max_length=120)
    chart_type: str = Field(
        ...,
        pattern="^(bar|line|donut|stacked_bar|histogram)$",
        description=(
            "Shape to render. bar/line/donut/stacked_bar use the first "
            "dimension as x-labels; histogram uses raw measure values."
        ),
    )
    measure: str = Field(...,
                         description="Same measure name accepted by query_data.")
    dimensions: List[str] = Field(
        default_factory=list, max_length=2,
        description=(
            "0-2 grouping dims (same names as query_data). Without dims "
            "you'll get a scalar — use 'histogram' to plot raw rows. With "
            "1 dim: x-axis. With 2 dims: stacked / multi-series."
        ),
    )
    filters: Dict[str, Any] = Field(default_factory=dict)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=500)
    order_by: Optional[str] = None
    order_dir: str = Field(default="desc", pattern="^(asc|desc)$")


def _chart_from_data(
    ctx: ToolContext, args: ChartFromDataArgs
) -> Dict[str, Any]:
    """Run a query_data spec, reshape rows into x/series, render chart.

    Saves the model from manually calling query_data, copying rows,
    and crafting an AdhocChartArgs payload. The chart artifact is
    registered for later retrieval via list_artifacts.
    """
    from app.ai_chat_layer.mcp_server import semantic
    spec = semantic.QuerySpec(
        measure=args.measure,
        dimensions=list(args.dimensions or []),
        filters=dict(args.filters or {}),
        date_from=args.date_from, date_to=args.date_to,
        limit=args.limit, order_by=args.order_by, order_dir=args.order_dir,
    )
    try:
        sql, params, expanding = semantic.build_sql(spec, ctx.scope)
    except semantic.SemanticError as exc:
        return {"error": str(exc)}
    try:
        rows = ctx.mcp.query(sql, params, expanding_keys=expanding or None)
    except Exception as exc:
        return {"error": f"query failed: {exc}"}
    if not rows:
        return {"rendered": False, "reason": "no_data",
                "spec": {"measure": args.measure,
                         "dimensions": list(args.dimensions),
                         "filters": dict(args.filters)}}

    measure_key = args.measure
    n_dims = len(args.dimensions)

    if n_dims == 0:
        # Scalar — nothing to chart unless histogram fed raw values.
        return {"rendered": False, "reason": "scalar_result",
                "rows": rows}
    if n_dims == 1:
        d0 = args.dimensions[0]
        x_labels = [str(r.get(d0)) if r.get(d0) is not None else "—"
                    for r in rows]
        values = [float(r.get(measure_key) or 0) for r in rows]
        adhoc = AdhocChartArgs(
            title=args.title, chart_type=args.chart_type,
            x_labels=x_labels,
            series=[AdhocSeries(name=measure_key, values=values)],
            y_label=measure_key,
        )
    else:
        # Two dims: pivot — d0 = x-axis, d1 = series.
        d0, d1 = args.dimensions
        x_set: List[str] = []
        x_seen = set()
        s_groups: Dict[str, Dict[str, float]] = {}
        for r in rows:
            x = str(r.get(d0)) if r.get(d0) is not None else "—"
            s = str(r.get(d1)) if r.get(d1) is not None else "—"
            v = float(r.get(measure_key) or 0)
            if x not in x_seen:
                x_set.append(x)
                x_seen.add(x)
            s_groups.setdefault(s, {})[x] = v
        series = [
            AdhocSeries(name=s_name,
                        values=[s_groups[s_name].get(x, 0.0) for x in x_set])
            for s_name in s_groups.keys()
        ]
        adhoc = AdhocChartArgs(
            title=args.title, chart_type=args.chart_type,
            x_labels=x_set, series=series, y_label=measure_key,
        )

    return _render_adhoc_chart(ctx, adhoc)


class _NoArgs(BaseModel):
    pass


def _list_chart_types(ctx: ToolContext, _: _NoArgs) -> Dict[str, Any]:
    return {
        "dashboard_chart_ids": sorted(KNOWN_CHART_IDS),
        "adhoc_chart_types": [
            {"type": "bar", "shape": "x_labels + 1+ series"},
            {"type": "stacked_bar", "shape": "x_labels + 2+ series (stacked)"},
            {"type": "line", "shape": "x_labels + 1+ series"},
            {"type": "donut", "shape": "x_labels + series[0].values"},
            {"type": "histogram",
             "shape": "raw values in series[0].values; bins controls buckets"},
            {"type": "heatmap",
             "shape": "x_labels + y_labels + matrix (rows×cols)"},
            {"type": "scatter",
             "shape": "x_values[i] paired with series[i].values"},
        ],
        "shortcut": (
            "For a single tool call from data → chart, prefer "
            "`chart_from_data(measure, dimensions, filters, chart_type)`."
        ),
    }


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
              ("Render an ad-hoc chart from explicit data you already have. "
               "Supported chart_types: bar, stacked_bar, line, donut, "
               "histogram, heatmap, scatter. Each shape has different "
               "args — see `list_chart_types` for hints. Stored as PNG "
               "in S3 + registered as an artifact. Use when no "
               "dashboard chart_id matches the request shape.")),
        _wrap("chart_from_data", ChartFromDataArgs, _chart_from_data,
              ("One-shot data-to-chart: pass a query_data-style spec "
               "(measure, dimensions[0-2], filters, dates) plus a "
               "chart_type, and the tool runs the analytics query AND "
               "renders the chart in a single call. With 1 dimension "
               "you get a normal bar/line/donut; with 2 dimensions the "
               "first becomes the x-axis and the second becomes "
               "stacked / grouped series. Use this whenever the user "
               "asks for a chart of data you'd otherwise have to "
               "fetch first.")),
        _wrap("list_chart_types", _NoArgs, _list_chart_types,
              ("Discoverability: returns the catalog of dashboard "
               "chart_ids and the supported ad-hoc chart_types with "
               "their data-shape hints. Call this when unsure which "
               "chart shape fits the question.")),
    ]
