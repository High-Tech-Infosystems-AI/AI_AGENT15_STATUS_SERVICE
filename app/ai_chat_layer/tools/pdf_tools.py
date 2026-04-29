"""PDF report generation. Mirrors the reportlab pattern from
AIAGENT14_JOB_AGENTS_SERVICE/app/services/exporters.py."""
from __future__ import annotations

import io
import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai_chat_layer.s3_helper import upload_ai_artifact
from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")


class PdfSection(BaseModel):
    heading: str = Field(..., max_length=200)
    body: str = Field(..., max_length=20000)
    bullet_points: List[str] = Field(default_factory=list, max_length=20)


class PdfReportArgs(BaseModel):
    title: str = Field(..., max_length=160)
    subtitle: Optional[str] = Field(default=None, max_length=200)
    sections: List[PdfSection] = Field(default_factory=list, max_length=20)


def _generate_pdf_report(ctx: ToolContext, args: PdfReportArgs) -> Dict[str, Any]:
    try:
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # type: ignore
        from reportlab.lib.units import inch  # type: ignore
        from reportlab.platypus import (  # type: ignore
            ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer,
        )
    except Exception as exc:
        return {"error": f"reportlab unavailable: {exc}"}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    subtitle_style = ParagraphStyle("Sub", parent=styles["Italic"], fontSize=11,
                                    textColor="#475569", spaceAfter=12)

    story: List[Any] = [Paragraph(args.title, title_style)]
    if args.subtitle:
        story.append(Paragraph(args.subtitle, subtitle_style))
    story.append(Spacer(1, 0.2 * inch))

    for sec in args.sections:
        story.append(Paragraph(sec.heading, h2))
        if sec.body:
            story.append(Paragraph(sec.body, body))
        if sec.bullet_points:
            items = [ListItem(Paragraph(b, body)) for b in sec.bullet_points]
            story.append(ListFlowable(items, bulletType="bullet"))
        story.append(Spacer(1, 0.15 * inch))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()

    key, url = upload_ai_artifact(
        data=pdf_bytes, mime="application/pdf",
        user_id=ctx.user_id, kind="report", ext="pdf",
    )
    artifact = {"kind": "report", "s3_key": key, "url": url,
                "mime": "application/pdf",
                "file_name": f"{args.title[:60].replace(' ', '_')}.pdf"}
    ctx.add_artifact("report", key, url, "application/pdf",
                     {"title": args.title})
    return {"rendered": True, "artifact": artifact}


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
            args = PdfReportArgs(**kwargs)
            out = _generate_pdf_report(ctx, args)
            ctx.add_trace("generate_pdf_report", kwargs,
                          int((time.monotonic() - start) * 1000), True)
            return out
        except Exception as exc:
            ctx.add_trace("generate_pdf_report", kwargs,
                          int((time.monotonic() - start) * 1000), False, str(exc))
            return {"error": str(exc)}

    return [StructuredTool.from_function(
        func=_runner,
        name="generate_pdf_report",
        description=("Build a multi-section PDF report from data the agent already "
                     "fetched. Stored in S3, surfaced as a downloadable attachment."),
        args_schema=PdfReportArgs,
    )]
