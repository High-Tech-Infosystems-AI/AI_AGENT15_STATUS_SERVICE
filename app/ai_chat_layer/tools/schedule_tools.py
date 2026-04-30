"""Scheduled-report + audit tools the agent can call.

These wrap the same business logic as the REST endpoints in
`api/schedules_api.py` and `api/audit_api.py`, so the model can drive
schedule lifecycle from chat ("schedule a weekly Monday 9am company
summary"), and admins can investigate what the bot has been asked
without leaving chat.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import desc

from app.ai_chat_layer.api.schedules_api import (
    _approver_role_for, _has_pending_approval, _next_run_at,
)
from app.ai_chat_layer.models import (
    AiApproval, AiQueryAudit, AiScheduledQuery,
)
from app.ai_chat_layer.tools.context import ToolContext
from app.chat_layer.chat_acl import is_admin

logger = logging.getLogger("app_logger")


def _is_super(user: Dict[str, Any]) -> bool:
    return (user.get("role_name") or "").lower() in {
        "super_admin", "superadmin", "super admin",
    }


# ─── schedule_report ────────────────────────────────────────────────

class ScheduleReportArgs(BaseModel):
    name: str = Field(..., max_length=120)
    prompt: str = Field(
        ..., max_length=10000,
        description=(
            "The exact natural-language prompt the agent should run on "
            "each schedule firing — e.g. 'Send me Q1 hiring numbers "
            "for Acme as a PDF'."
        ),
    )
    cron_expr: str = Field(
        ..., max_length=64,
        description="UTC cron, e.g. '0 9 * * 1' (Mondays 9am UTC).",
    )
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Tagged entity refs to preserve into the recurring run "
            "(same shape as the user's tagged refs in the chat)."
        ),
    )


def _schedule_report(ctx: ToolContext, args: ScheduleReportArgs) -> Dict[str, Any]:
    approver_role = _approver_role_for(ctx.user)
    auto_active = approver_role == "self"
    sched = AiScheduledQuery(
        user_id=ctx.user_id,
        name=args.name, prompt=args.prompt,
        refs=args.refs or None,
        cron_expr=args.cron_expr, timezone=args.timezone,
        is_active=1 if auto_active else 0,
        next_run_at=_next_run_at(args.cron_expr) if auto_active else None,
    )
    ctx.db.add(sched)
    ctx.db.commit()
    ctx.db.refresh(sched)

    if not auto_active:
        approval = AiApproval(
            user_id=ctx.user_id,
            origin="schedule_create",
            payload={"schedule_id": sched.id, "name": sched.name,
                     "cron_expr": sched.cron_expr},
            approver_role=approver_role,
            target_kind="schedule", target_id=sched.id,
        )
        ctx.db.add(approval)
        ctx.db.commit()
    return {
        "scheduled": True,
        "id": sched.id,
        "name": sched.name,
        "cron_expr": sched.cron_expr,
        "is_active": bool(sched.is_active),
        "pending_approval": not auto_active,
        "approver_role": approver_role,
        "next_run_at": (
            sched.next_run_at.isoformat() if sched.next_run_at else None
        ),
        "note": (
            "Schedule created and is now active." if auto_active else
            f"Schedule created, awaiting approval from {approver_role}."
        ),
    }


# ─── list_scheduled_reports ─────────────────────────────────────────

class ListSchedulesArgs(BaseModel):
    active_only: bool = Field(default=False)
    limit: int = Field(default=50, ge=1, le=200)


def _list_scheduled_reports(ctx: ToolContext, args: ListSchedulesArgs) -> Dict[str, Any]:
    q = ctx.db.query(AiScheduledQuery).filter(
        AiScheduledQuery.user_id == ctx.user_id,
    )
    if args.active_only:
        q = q.filter(AiScheduledQuery.is_active == 1)
    rows = q.order_by(desc(AiScheduledQuery.created_at)).limit(args.limit).all()
    items = []
    for r in rows:
        items.append({
            "id": r.id, "name": r.name,
            "prompt": r.prompt[:300],
            "cron_expr": r.cron_expr, "timezone": r.timezone,
            "is_active": bool(r.is_active),
            "pending_approval": _has_pending_approval(ctx.db, r.id),
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"items": items, "count": len(items)}


# ─── pause / resume / delete ────────────────────────────────────────

class ScheduleIdArgs(BaseModel):
    schedule_id: int


def _toggle_schedule(ctx: ToolContext, schedule_id: int, active: bool) -> Dict[str, Any]:
    row = ctx.db.get(AiScheduledQuery, schedule_id)
    if not row or row.user_id != ctx.user_id:
        return {"not_found": True, "id": schedule_id}
    if active and _has_pending_approval(ctx.db, schedule_id):
        return {
            "blocked": True,
            "reason": "pending_approval",
            "note": "Schedule is awaiting approval and cannot be activated yet.",
        }
    row.is_active = 1 if active else 0
    if active:
        row.next_run_at = _next_run_at(row.cron_expr)
    ctx.db.commit()
    ctx.db.refresh(row)
    return {
        "id": row.id, "name": row.name,
        "is_active": bool(row.is_active),
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
    }


def _pause_schedule(ctx: ToolContext, args: ScheduleIdArgs) -> Dict[str, Any]:
    return _toggle_schedule(ctx, args.schedule_id, active=False)


def _resume_schedule(ctx: ToolContext, args: ScheduleIdArgs) -> Dict[str, Any]:
    return _toggle_schedule(ctx, args.schedule_id, active=True)


def _delete_schedule(ctx: ToolContext, args: ScheduleIdArgs) -> Dict[str, Any]:
    row = ctx.db.get(AiScheduledQuery, args.schedule_id)
    if not row or row.user_id != ctx.user_id:
        return {"not_found": True, "id": args.schedule_id}
    ctx.db.delete(row)
    ctx.db.query(AiApproval).filter(
        AiApproval.target_kind == "schedule",
        AiApproval.target_id == args.schedule_id,
        AiApproval.status == "pending",
    ).update({"status": "expired"})
    ctx.db.commit()
    return {"deleted": True, "id": args.schedule_id}


# ─── run_scheduled_report ───────────────────────────────────────────

def _run_scheduled_report(ctx: ToolContext, args: ScheduleIdArgs) -> Dict[str, Any]:
    """Synchronously run one schedule once. Skips approval / activeness
    gates so the user can manually trigger a paused or pending schedule
    without changing its persistent state.
    """
    row = ctx.db.get(AiScheduledQuery, args.schedule_id)
    if not row or row.user_id != ctx.user_id:
        return {"not_found": True, "id": args.schedule_id}

    # Lazy imports to avoid circular dependencies on the agent module.
    from app.ai_chat_layer import agent as ai_agent
    from app.ai_chat_layer.api.dm import get_or_create_ai_dm

    try:
        conv_id, _bot = get_or_create_ai_dm(ctx.db, ctx.user_id)
        ai_agent.run_turn(
            db=ctx.db, user=ctx.user,
            prompt=row.prompt,
            refs=row.refs or [],
            conversation_id=conv_id,
            ip_address=None,
        )
        row.last_run_at = datetime.utcnow()
        ctx.db.commit()
        return {
            "ran": True,
            "id": row.id,
            "name": row.name,
            "last_run_at": row.last_run_at.isoformat(),
            "note": "Reply posted to your AI Assistant thread.",
        }
    except Exception as exc:
        logger.exception("manual run of schedule %s failed", row.id)
        return {"ran": False, "id": row.id, "error": str(exc)}


# ─── search_audit (admin-only) ──────────────────────────────────────

class SearchAuditArgs(BaseModel):
    text: Optional[str] = Field(
        default=None, max_length=200,
        description="Substring match against the prompt (case-insensitive).",
    )
    user_id: Optional[int] = Field(default=None)
    status: Optional[str] = Field(
        default=None,
        description="'ok' / 'error' / 'rejected_quota' / 'rejected_acl'.",
    )
    since_days: Optional[int] = Field(default=None, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=100)


def _search_audit(ctx: ToolContext, args: SearchAuditArgs) -> Dict[str, Any]:
    if not (is_admin(ctx.user.get("role_name")) or _is_super(ctx.user)):
        return {
            "access_denied": True,
            "note": "Audit search is admin-only.",
        }

    q = ctx.db.query(AiQueryAudit)
    if args.text:
        like = f"%{args.text.strip()}%"
        q = q.filter(AiQueryAudit.prompt.like(like))
    if args.user_id is not None:
        q = q.filter(AiQueryAudit.user_id == int(args.user_id))
    if args.status:
        q = q.filter(AiQueryAudit.status == args.status)
    if args.since_days:
        cutoff = datetime.utcnow() - timedelta(days=int(args.since_days))
        q = q.filter(AiQueryAudit.created_at >= cutoff)
    rows = q.order_by(desc(AiQueryAudit.created_at)).limit(args.limit).all()
    items = []
    for r in rows:
        tools = []
        if r.tools_called:
            for t in r.tools_called[:8]:
                if isinstance(t, dict):
                    tools.append(t.get("name") or "?")
        items.append({
            "id": r.id,
            "user_id": r.user_id,
            "conversation_id": r.conversation_id,
            "prompt": (r.prompt or "")[:300],
            "model": r.model,
            "prompt_version": r.prompt_version,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "latency_ms": r.latency_ms,
            "status": r.status,
            "error_msg": r.error_msg,
            "tools_called": tools,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"items": items, "count": len(items)}


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
        _wrap("schedule_report", ScheduleReportArgs, _schedule_report,
              ("Create a recurring report schedule. The given `prompt` "
               "is re-run by the agent on each cron firing under the "
               "caller's identity, and the answer is posted to their "
               "AI Assistant thread. Approval workflow: SuperAdmins are "
               "auto-approved; Admins need SuperAdmin sign-off; "
               "Recruiters need Admin or SuperAdmin sign-off — the "
               "tool returns `pending_approval=true` when the schedule "
               "is dormant pending review. Use this for 'send me weekly "
               "Monday 9am hiring summary' / 'monthly Q1 PDF for "
               "Acme'.")),
        _wrap("list_scheduled_reports", ListSchedulesArgs,
              _list_scheduled_reports,
              ("List the caller's recurring report schedules with "
               "is_active, pending_approval, last_run_at, next_run_at. "
               "Optional `active_only` filter. Use for 'what reports do "
               "I have scheduled' / 'show my recurring reports'.")),
        _wrap("pause_scheduled_report", ScheduleIdArgs, _pause_schedule,
              ("Pause one of the caller's schedules — sets is_active=0 "
               "so it stops firing. Reversible via "
               "`resume_scheduled_report`.")),
        _wrap("resume_scheduled_report", ScheduleIdArgs, _resume_schedule,
              ("Resume a paused schedule — sets is_active=1 and "
               "materializes the next next_run_at. Blocked if the "
               "schedule still has a pending approval.")),
        _wrap("delete_scheduled_report", ScheduleIdArgs, _delete_schedule,
              ("Permanently delete one of the caller's schedules. Any "
               "pending approval on it is marked 'expired'. Use for "
               "'cancel my Monday schedule' / 'remove that report'. "
               "NOT reversible.")),
        _wrap("run_scheduled_report", ScheduleIdArgs, _run_scheduled_report,
              ("Manually re-run a saved schedule once, right now, "
               "without changing its is_active state or cron cadence. "
               "Useful for 'run my weekly report now' / 'preview that "
               "schedule before activating'.")),
        _wrap("search_audit", SearchAuditArgs, _search_audit,
              ("Admin-only — search the AI query audit log. Filter by "
               "prompt substring (`text`), `user_id`, `status` ('ok' / "
               "'error' / 'rejected_quota' / 'rejected_acl'), and "
               "`since_days`. Returns prompt + model + token counts + "
               "latency + tools_called for each row, ordered "
               "newest-first. Use for 'who asked about Acme last "
               "week', 'show me failed AI runs', token-cost audits.")),
    ]
