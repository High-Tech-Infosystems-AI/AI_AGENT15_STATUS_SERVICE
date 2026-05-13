"""PDF report generation. Mirrors the reportlab pattern from
AIAGENT14_JOB_AGENTS_SERVICE/app/services/exporters.py.

Sections can carry:
  * `body` — paragraph text
  * `bullet_points` — bulleted list
  * `tables` — headers + rows, rendered as gridded reportlab Tables
  * `chart_artifacts` — S3 keys returned from a previous
    `render_adhoc_chart` call. The PDF builder fetches each PNG and
    inlines it as an Image.
"""
from __future__ import annotations

import io
import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai_chat_layer.s3_helper import (
    download_ai_artifact, register_ai_artifact, upload_ai_artifact,
)
from app.ai_chat_layer.tools.context import ToolContext

logger = logging.getLogger("app_logger")


class PdfTable(BaseModel):
    headers: List[str] = Field(..., min_length=1, max_length=12)
    rows: List[List[str]] = Field(default_factory=list, max_length=200)
    caption: Optional[str] = Field(default=None, max_length=200)


class PdfSection(BaseModel):
    heading: str = Field(..., max_length=200)
    body: str = Field(default="", max_length=20000)
    bullet_points: List[str] = Field(default_factory=list, max_length=20)
    tables: List[PdfTable] = Field(default_factory=list, max_length=10)
    chart_artifacts: List[str] = Field(
        default_factory=list, max_length=8,
        description="S3 keys from prior render_adhoc_chart calls.",
    )


class PdfReportArgs(BaseModel):
    title: str = Field(..., max_length=160)
    subtitle: Optional[str] = Field(default=None, max_length=200)
    sections: List[PdfSection] = Field(default_factory=list, max_length=20)


def _generate_pdf_report(ctx: ToolContext, args: PdfReportArgs) -> Dict[str, Any]:
    try:
        from reportlab.lib import colors  # type: ignore
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # type: ignore
        from reportlab.lib.units import inch  # type: ignore
        from reportlab.platypus import (  # type: ignore
            Image, ListFlowable, ListItem, Paragraph,
            SimpleDocTemplate, Spacer, Table, TableStyle,
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
    caption_style = ParagraphStyle("Cap", parent=styles["Italic"], fontSize=9,
                                    textColor="#475569", spaceAfter=6)
    subtitle_style = ParagraphStyle("Sub", parent=styles["Italic"], fontSize=11,
                                    textColor="#475569", spaceAfter=12)

    page_w = LETTER[0] - 1.2 * inch  # available content width

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
            story.append(Spacer(1, 0.08 * inch))

        for tbl in sec.tables:
            data = [list(tbl.headers)] + [
                [str(c) if c is not None else "" for c in row]
                for row in tbl.rows
            ]
            n_cols = len(tbl.headers)
            col_w = page_w / n_cols if n_cols else page_w
            t = Table(data, colWidths=[col_w] * n_cols, repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#f1f5f9")]),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            if tbl.caption:
                story.append(Paragraph(tbl.caption, caption_style))
            story.append(t)
            story.append(Spacer(1, 0.12 * inch))

        for s3_key in sec.chart_artifacts:
            try:
                png = download_ai_artifact(s3_key)
            except Exception as exc:
                logger.warning("inline chart fetch failed for %s: %s", s3_key, exc)
                continue
            if not png:
                continue
            img_buf = io.BytesIO(png)
            try:
                img = Image(img_buf)
                # Scale to page width, preserve aspect ratio.
                w, h = img.imageWidth, img.imageHeight
                if w and h:
                    target_w = min(page_w, w)
                    img.drawWidth = target_w
                    img.drawHeight = h * (target_w / w)
                story.append(img)
                story.append(Spacer(1, 0.12 * inch))
            except Exception as exc:
                logger.warning("inline chart render failed: %s", exc)

        story.append(Spacer(1, 0.15 * inch))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()

    file_name = f"{args.title[:60].replace(' ', '_')}.pdf"
    key, url = upload_ai_artifact(
        data=pdf_bytes, mime="application/pdf",
        user_id=ctx.user_id, kind="report", ext="pdf",
    )
    artifact_id = register_ai_artifact(
        db=ctx.db, user_id=ctx.user_id, kind="report",
        s3_key=key, mime="application/pdf",
        file_name=file_name, title=args.title,
        meta={"sections": len(args.sections)},
    )
    artifact = {"kind": "report", "s3_key": key, "url": url,
                "mime": "application/pdf",
                "file_name": file_name,
                "artifact_id": artifact_id}
    ctx.add_artifact("report", key, url, "application/pdf",
                     {"title": args.title, "artifact_id": artifact_id})
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
        description=(
            "Build a multi-section PDF report from data the agent already "
            "fetched. Each section can carry body text, bullet points, "
            "data TABLES (headers + rows; great for top-N candidates / "
            "company breakdowns / leaderboards), and CHART images "
            "(`chart_artifacts` — S3 keys returned by a previous "
            "`render_adhoc_chart` call; the PDF inlines each chart "
            "PNG). The finished PDF is stored in S3, registered in the "
            "artifact registry (so `list_artifacts` / `get_artifact_url` "
            "can find it later), and surfaced as a downloadable "
            "attachment in the chat reply."
        ),
        args_schema=PdfReportArgs,
    )]
