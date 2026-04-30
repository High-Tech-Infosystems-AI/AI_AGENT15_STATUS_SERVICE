"""Lightweight export tools — CSV + Markdown — and the artifact
registry tools (`list_artifacts`, `get_artifact_url`).

CSVs and markdown reports are persisted to the same S3 bucket as PDFs
and recorded in the `ai_artifact` table so users can rediscover them
later. Markdown is the shareable format users requested — easy to
paste into Slack / Notion / GitHub comments without a download step.
"""
from __future__ import annotations

import csv as _csv
import io
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import desc

from app.ai_chat_layer.s3_helper import (
    register_ai_artifact, upload_ai_artifact,
)
from app.ai_chat_layer.tools.context import ToolContext
from app.chat_layer.s3_chat_service import presign_get

logger = logging.getLogger("app_logger")


# ─── CSV export ──────────────────────────────────────────────────────

class CsvExportArgs(BaseModel):
    title: str = Field(..., max_length=160)
    columns: List[str] = Field(..., min_length=1, max_length=40)
    rows: List[List[Any]] = Field(default_factory=list, max_length=5000)


def _export_csv(ctx: ToolContext, args: CsvExportArgs) -> Dict[str, Any]:
    buf = io.StringIO()
    w = _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL)
    w.writerow(args.columns)
    for row in args.rows:
        w.writerow(["" if c is None else str(c) for c in row])
    data = buf.getvalue().encode("utf-8")

    file_name = f"{args.title[:80].replace(' ', '_')}.csv"
    s3_key, url = upload_ai_artifact(
        data=data, mime="text/csv",
        user_id=ctx.user_id, kind="csv", ext="csv",
    )
    artifact_id = register_ai_artifact(
        db=ctx.db, user_id=ctx.user_id, kind="csv",
        s3_key=s3_key, mime="text/csv",
        file_name=file_name, title=args.title,
        meta={"columns": args.columns, "row_count": len(args.rows)},
    )
    artifact = {"kind": "csv", "s3_key": s3_key, "url": url,
                "mime": "text/csv", "file_name": file_name,
                "artifact_id": artifact_id}
    ctx.add_artifact("csv", s3_key, url, "text/csv",
                     {"title": args.title, "row_count": len(args.rows),
                      "artifact_id": artifact_id})
    return {"rendered": True, "artifact": artifact,
            "row_count": len(args.rows)}


# ─── Markdown export ────────────────────────────────────────────────

class MarkdownExportArgs(BaseModel):
    title: str = Field(..., max_length=160)
    content: str = Field(
        ..., max_length=200000,
        description=(
            "Full markdown body. The model authors this directly — "
            "include headings, paragraphs, bullet lists, and "
            "GitHub-flavored markdown tables as needed. The H1 title "
            "is prepended automatically; do NOT include it in `content`."
        ),
    )


def _export_markdown(ctx: ToolContext, args: MarkdownExportArgs) -> Dict[str, Any]:
    body = args.content.lstrip()
    md = f"# {args.title}\n\n{body}\n"
    data = md.encode("utf-8")

    file_name = f"{args.title[:80].replace(' ', '_')}.md"
    s3_key, url = upload_ai_artifact(
        data=data, mime="text/markdown",
        user_id=ctx.user_id, kind="markdown", ext="md",
    )
    artifact_id = register_ai_artifact(
        db=ctx.db, user_id=ctx.user_id, kind="markdown",
        s3_key=s3_key, mime="text/markdown",
        file_name=file_name, title=args.title,
        meta={"length": len(md)},
    )
    artifact = {"kind": "markdown", "s3_key": s3_key, "url": url,
                "mime": "text/markdown", "file_name": file_name,
                "artifact_id": artifact_id}
    ctx.add_artifact("markdown", s3_key, url, "text/markdown",
                     {"title": args.title, "artifact_id": artifact_id})
    # Surface the rendered markdown inline so the chat reply can show
    # it immediately — the model is encouraged to include the same
    # markdown in the visible reply too.
    return {"rendered": True, "artifact": artifact,
            "preview": body[:1500]}


# ─── Artifact registry ──────────────────────────────────────────────

class ListArtifactsArgs(BaseModel):
    kind: Optional[str] = Field(
        default=None,
        description="Filter by kind: 'report' / 'chart' / 'csv' / 'markdown'.",
    )
    since_days: Optional[int] = Field(
        default=None, ge=1, le=365,
        description="Only artifacts created within the last N days.",
    )
    limit: int = Field(default=20, ge=1, le=100)


def _list_artifacts(ctx: ToolContext, args: ListArtifactsArgs) -> Dict[str, Any]:
    try:
        from app.ai_chat_layer.models import AiArtifact
    except Exception as exc:
        return {"error": f"artifact registry unavailable: {exc}", "items": []}

    q = ctx.db.query(AiArtifact).filter(
        AiArtifact.user_id == ctx.user_id,
    )
    if args.kind:
        q = q.filter(AiArtifact.kind == args.kind.lower())
    if args.since_days:
        cutoff = datetime.utcnow() - timedelta(days=int(args.since_days))
        q = q.filter(AiArtifact.created_at >= cutoff)
    rows = q.order_by(desc(AiArtifact.created_at)).limit(args.limit).all()
    items = []
    for r in rows:
        items.append({
            "artifact_id": r.id,
            "kind": r.kind,
            "title": r.title,
            "file_name": r.file_name,
            "mime": r.mime,
            "s3_key": r.s3_key,
            "created_at": (
                r.created_at.isoformat() if r.created_at else None
            ),
            # NOTE: presigned URL not issued here to keep the response
            # cheap; call `get_artifact_url(artifact_id)` to fetch one.
            "meta": r.meta,
        })
    return {"items": items, "count": len(items)}


class GetArtifactUrlArgs(BaseModel):
    artifact_id: int


def _get_artifact_url(ctx: ToolContext, args: GetArtifactUrlArgs) -> Dict[str, Any]:
    try:
        from app.ai_chat_layer.models import AiArtifact
    except Exception as exc:
        return {"error": f"artifact registry unavailable: {exc}"}

    row = ctx.db.query(AiArtifact).filter(
        AiArtifact.id == args.artifact_id,
        AiArtifact.user_id == ctx.user_id,
    ).first()
    if not row:
        return {"not_found": True, "artifact_id": args.artifact_id}
    url = presign_get(row.s3_key)
    return {
        "artifact_id": row.id,
        "kind": row.kind,
        "title": row.title,
        "file_name": row.file_name,
        "mime": row.mime,
        "s3_key": row.s3_key,
        "url": url,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ─── Tool builder ───────────────────────────────────────────────────

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
                ctx.add_trace(name, kwargs,
                              int((time.monotonic() - start) * 1000), True)
                return out
            except Exception as exc:
                ctx.add_trace(name, kwargs,
                              int((time.monotonic() - start) * 1000),
                              False, str(exc))
                logger.exception("tool %s failed", name)
                return {"error": str(exc)}
        return StructuredTool.from_function(
            func=_runner, name=name, description=description,
            args_schema=args_schema,
        )

    return [
        _wrap("export_csv", CsvExportArgs, _export_csv,
              ("Persist tabular data the agent already fetched as a CSV "
               "in S3 and surface a downloadable attachment + signed URL. "
               "Pass `columns` (header row) and `rows` (list of lists, "
               "values can be any scalar). Great for 'export top 50 "
               "candidates as CSV', 'download all jobs at company X', "
               "etc. The artifact is also registered so "
               "`list_artifacts` / `get_artifact_url` can find it later.")),
        _wrap("export_markdown", MarkdownExportArgs, _export_markdown,
              ("Persist a Markdown document the agent has authored as a "
               "shareable .md file in S3. Write the FULL content "
               "yourself — including GitHub-flavored markdown tables "
               "(`| col | … |\\n|---|---|`), bullet lists, headings — "
               "and pass it as `content`. The H1 title is prepended "
               "automatically. Use this whenever the user asks for a "
               "downloadable / shareable summary that's lighter than a "
               "full PDF. The artifact is registered so it can be "
               "recovered later via `get_artifact_url`.")),
        _wrap("list_artifacts", ListArtifactsArgs, _list_artifacts,
              ("List the caller's previously generated artifacts (PDF "
               "reports, charts, CSVs, markdown exports) ordered "
               "newest-first. Optional filters: `kind` ('report' / "
               "'chart' / 'csv' / 'markdown'), `since_days` (window). "
               "Returns artifact_id + title + file_name + created_at "
               "but NOT a presigned URL (which expires); call "
               "`get_artifact_url(artifact_id)` to fetch one when the "
               "user wants to re-download.")),
        _wrap("get_artifact_url", GetArtifactUrlArgs, _get_artifact_url,
              ("Re-issue a fresh signed URL for one of the caller's "
               "prior artifacts (looked up by artifact_id from "
               "`list_artifacts`). Use this whenever the user says "
               "'send me the link again' / 'redownload that report'.")),
    ]
