"""AI agent — drives a single Q&A turn end-to-end.

Pipeline (synchronous, runs in a thread for FastAPI):

    1. resolve refs → access middleware
    2. build session prompt (system + summary + recent turns + tagged refs)
    3. bind tools → Gemini Pro with function calling
    4. loop: model emits tool calls → execute → feed results back
       (max iterations = settings.AI_MAX_TOOL_ITER)
    5. final composed answer + collected refs + artifacts
    6. persist as a chat message from the AI bot user
    7. log audit + commit token usage

LangGraph is overkill for this linear shape, so we run the loop directly.
The `agent` API still mirrors a graph state for easy migration later.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app.ai_chat_layer import audit, llm, quota, session as ai_session, system_bot
from app.ai_chat_layer.access_middleware import (
    AccessDeniedError, apply_scope, assert_can_see_ref,
)
from app.ai_chat_layer.mcp_client import McpClient
from app.ai_chat_layer.prompts.system import (
    PROMPT_VERSION, render_qa_system, render_tags_block,
)
from app.ai_chat_layer.tools import get_registry
from app.ai_chat_layer.tools.context import ToolContext
from app.chat_layer import store as chat_store
from app.chat_layer.entity_resolver import resolve as resolve_entities
from app.core import settings

logger = logging.getLogger("app_logger")


def _today_iso() -> str:
    return datetime.utcnow().date().isoformat()


def _coerce_text(content: Any) -> str:
    """Normalize a LangChain message `content` into a plain string.

    Gemini can return the model output in three shapes:
      - a plain string,
      - a list of content blocks (dicts) like
            [{"type": "text", "text": "..."}, ...],
      - a list of strings (rare, from streaming concatenations).

    We collapse all three to a stripped string so downstream code can
    reliably index into it.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                t = block.get("text") or block.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


def _resolve_ref_cards(db: Session, refs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Look up the EntityCard for each ref so we can show titles in the prompt."""
    if not refs:
        return []
    try:
        cards = resolve_entities(
            db, [{"type": r.get("type"), "id": r.get("id"), "params": r.get("params")}
                 for r in refs],
        )
    except Exception as exc:
        logger.warning("ref resolve failed: %s", exc)
        return []
    out = []
    for card in cards or []:
        if not card:
            continue
        if isinstance(card, dict):
            out.append(card)
        else:
            out.append(card.dict() if hasattr(card, "dict") else dict(card))
    return out


def _collect_message_refs(refs_for_output: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert the agent's output_refs into the schema chat_messages.refs accepts."""
    out: List[Dict[str, Any]] = []
    for r in refs_for_output:
        out.append({
            "type": r.get("type"),
            "id": r.get("id"),
            "params": r.get("params"),
        })
    return out


TOOL_LABELS: Dict[str, str] = {
    "list_jobs": "Listing jobs",
    "job_detail": "Fetching job details",
    "candidate_detail": "Fetching candidate details",
    "list_candidates": "Looking up candidates",
    "pipeline_status_for_job": "Reading pipeline status",
    "pipeline_stages_for_job": "Reading pipeline structure",
    "pipeline_funnel": "Building the pipeline funnel",
    "count_candidates_by_stage": "Counting candidates by stage",
    "recruiter_metrics": "Computing recruiter metrics",
    "top_recruiters": "Building recruiter leaderboard",
    "users_for_job": "Listing recruiters on this job",
    "user_detail": "Fetching user profile",
    "compare_users": "Comparing users",
    "user_sourcing": "Listing candidates this user sourced",
    "team_detail": "Fetching team and members",
    "team_performance": "Building team performance",
    "company_detail": "Fetching company details",
    "company_jobs": "Listing jobs at this company",
    "company_performance": "Building company performance",
    "company_jobs_summary": "Summarizing companies",
    "search_entities": "Searching",
    "dashboard_data": "Preparing chart",
    "render_chart": "Preparing chart",
    "render_adhoc_chart": "Drawing chart",
    "generate_pdf_report": "Generating PDF report",
    "whatif_throughput": "Running what-if simulation",
    "suggest_followups": "Adding follow-up suggestions",
}


def run_turn(
    *,
    db: Session,
    user: Dict[str, Any],
    prompt: str,
    refs: Optional[List[Dict[str, Any]]],
    conversation_id: int,
    ip_address: Optional[str] = None,
    stream_cb: Optional[Callable[[str], None]] = None,
    refs_cb: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    status_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Execute one turn. Returns the persisted message + metadata."""
    user_id = int(user.get("user_id"))
    refs = refs or []
    started = time.monotonic()
    audit_status = "ok"
    audit_error: Optional[str] = None
    tokens_in = 0
    tokens_out = 0
    final_text = ""
    msg_id: Optional[int] = None
    output_refs: List[Dict[str, Any]] = []
    artifacts: List[Dict[str, Any]] = []
    trace: List[Dict[str, Any]] = []
    model_name = settings.GEMINI_PRO_MODEL

    # ---- 0. Persist the user's prompt as a real chat message ----
    # Without this the user never sees their own typed text echoed in the
    # AI thread (only the bot's reply is otherwise written). The broadcast
    # also fires `message.new` + `inbox.bump` so the FE clears its local
    # placeholder and renders the real row.
    user_msg_id = _post_user_message(db, conversation_id, user_id, prompt, refs)


    # ---- 1. Quota probe ----
    est = llm.estimate_tokens(prompt) + 256  # cushion for system/turns
    try:
        quota.check(db, user_id, est)
    except quota.QuotaExceededError as exc:
        audit_status = "rejected_quota"
        audit_error = str(exc)
        final_text = (
            f"You have hit your {exc.scope} AI token limit "
            f"({exc.used}/{exc.limit}). Try again later or ask a SuperAdmin "
            "to raise your limit."
        )
        msg_id = _post_reply(db, conversation_id, user_id, final_text, [], [])
        audit.log_query(
            db, user_id=user_id, prompt=prompt, model=model_name,
            prompt_version=PROMPT_VERSION, status=audit_status,
            error_msg=audit_error, conversation_id=conversation_id,
            refs=refs, tools_called=trace, ip_address=ip_address,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return {"message_id": msg_id, "text": final_text, "trace": trace,
                "refs": [], "artifacts": [],
                "user_message_id": user_msg_id}

    # ---- 2. Access guard for tagged refs ----
    scope = apply_scope(db, user)
    try:
        for r in refs:
            assert_can_see_ref(scope, r)
    except AccessDeniedError as exc:
        audit_status = "rejected_acl"
        audit_error = str(exc)
        final_text = (f"You do not have access to {exc.entity_type} {exc.entity_id}. "
                      "Try a different reference.")
        msg_id = _post_reply(db, conversation_id, user_id, final_text, [], [])
        audit.log_query(
            db, user_id=user_id, prompt=prompt, model=model_name,
            prompt_version=PROMPT_VERSION, status=audit_status,
            error_msg=audit_error, conversation_id=conversation_id,
            refs=refs, tools_called=trace, ip_address=ip_address,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return {"message_id": msg_id, "text": final_text, "trace": trace,
                "refs": [], "artifacts": [],
                "user_message_id": user_msg_id}

    # ---- 3. Build context + tools ----
    ctx = ToolContext(
        db=db, user=user, scope=scope, mcp=McpClient(db), refs=refs,
    )
    cards = _resolve_ref_cards(db, refs)
    tools = get_registry(ctx)

    # ---- 4. Memory + system prompt ----
    state = ai_session.load(user_id, conversation_id)
    summary = state.get("summary") or ""
    history_turns = state.get("turns") or []
    sys_text = render_qa_system(
        today=_today_iso(),
        tags=render_tags_block(cards),
        summary=summary,
    )

    # ---- 5. Drive the model ----
    try:
        client = llm.pro_llm()
        if client is None:
            raise RuntimeError(
                "Gemini client unavailable. Install langchain_google_genai "
                "and set GEMINI_API_KEY.",
            )
        client = client.bind_tools(tools) if tools else client

        from langchain_core.messages import (  # type: ignore
            AIMessage, HumanMessage, SystemMessage, ToolMessage,
        )

        messages: List[Any] = [SystemMessage(content=sys_text)]
        for t in history_turns[-10:]:
            role = t.get("role")
            content = t.get("content") or ""
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
        messages.append(HumanMessage(content=prompt))

        max_iter = int(getattr(settings, "AI_MAX_TOOL_ITER", 5) or 5)
        tool_lookup = {getattr(t, "name", ""): t for t in tools}

        # Track which output_refs we've already pushed to the FE so we
        # only stream the deltas (e.g. when a new chart card lands).
        refs_pushed = 0

        def _stream_once(messages_so_far):
            """Stream the model's next message; return the assembled
            AIMessage / AIMessageChunk-equivalent so the rest of the loop
            can read tool_calls + content like before.

            For each text fragment we get, push it onto the FE via the
            stream_cb so the UI bubble grows live.
            """
            assembled = None
            text_so_far = ""
            try:
                stream_iter = client.stream(messages_so_far)
            except Exception:
                # Provider doesn't support .stream() on bound tools — fall
                # back to a single .invoke() call.
                resp = client.invoke(messages_so_far)
                content_text = _coerce_text(getattr(resp, "content", None))
                if stream_cb and content_text:
                    try:
                        stream_cb(content_text)
                    except Exception:
                        pass
                return resp
            for chunk in stream_iter:
                # AIMessageChunk supports `chunk + chunk` to accumulate.
                assembled = chunk if assembled is None else assembled + chunk
                fragment = _coerce_text(getattr(chunk, "content", None))
                if fragment:
                    text_so_far += fragment
                    if stream_cb:
                        try:
                            stream_cb(fragment)
                        except Exception:
                            logger.exception("stream_cb failed")
            return assembled

        for _ in range(max_iter):
            response = _stream_once(messages)
            usage = llm.usage_metadata(response)
            tokens_in += usage.get("tokens_in", 0)
            tokens_out += usage.get("tokens_out", 0)
            messages.append(response)
            calls = getattr(response, "tool_calls", None) or []
            if not calls:
                final_text = _coerce_text(getattr(response, "content", None))
                break
            # Execute each requested tool, append ToolMessage results.
            for call in calls:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
                args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
                call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                # Surface a human-friendly status to the UI so the user
                # sees "Fetching job details…" instead of a static
                # "Thinking…" while we hit the data layer.
                if status_cb:
                    label = TOOL_LABELS.get(name or "", "Working") + "…"
                    try:
                        status_cb(label)
                    except Exception:
                        logger.exception("status_cb failed")
                tool = tool_lookup.get(name)
                if tool is None:
                    out_payload = {"error": f"unknown tool: {name}"}
                else:
                    try:
                        out_payload = tool.invoke(args or {})
                    except Exception as exc:
                        out_payload = {"error": str(exc)}
                messages.append(ToolMessage(
                    content=str(out_payload),
                    tool_call_id=call_id or name or "tool",
                ))
            # After the tool round, hint that we're composing the answer
            # — this is the bridge state between data fetch and streaming.
            if calls and status_cb:
                try:
                    status_cb("Composing reply…")
                except Exception:
                    pass
            # After each tool round, surface any newly added refs so the
            # FE can render entity / chart cards mid-stream — the user
            # sees the cards appear as soon as their tool produces them.
            if refs_cb and len(ctx.output_refs) > refs_pushed:
                try:
                    refs_cb(ctx.output_refs[refs_pushed:])
                except Exception:
                    logger.exception("refs_cb failed")
                refs_pushed = len(ctx.output_refs)
        else:
            # Loop exhausted without a final composed reply.
            final_text = (final_text
                          or "I needed more steps than I'm allowed to take in one turn. "
                             "Try narrowing the question.")
    except Exception as exc:
        audit_status = "error"
        audit_error = str(exc)
        logger.exception("agent run failed")
        final_text = ("Sorry — I hit an error trying to answer that. "
                      "Please try again in a moment.")

    # Mirror tool-trace + artifacts + collected refs from ctx.
    trace = list(ctx.trace)
    artifacts = list(ctx.artifacts)
    output_refs = list(ctx.output_refs)

    # ---- 6. Persist as chat message + audit ----
    msg_id = _post_reply(db, conversation_id, user_id, final_text,
                        _collect_message_refs(output_refs), artifacts)

    quota.commit(db, user_id, tokens_in + tokens_out)
    ai_session.append_turn(user_id, conversation_id, "user", prompt)
    ai_session.append_turn(user_id, conversation_id, "assistant", final_text)

    audit.log_query(
        db, user_id=user_id, prompt=prompt, model=model_name,
        prompt_version=PROMPT_VERSION, status=audit_status,
        error_msg=audit_error, conversation_id=conversation_id,
        refs=refs, tools_called=trace,
        tokens_in=tokens_in, tokens_out=tokens_out,
        latency_ms=int((time.monotonic() - started) * 1000),
        ip_address=ip_address,
    )

    return {
        "message_id": msg_id,
        "user_message_id": user_msg_id,
        "text": final_text,
        "trace": trace,
        "refs": output_refs,
        "artifacts": artifacts,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def _broadcast_message(db: Session, msg, conversation_id: int) -> None:
    """Push a freshly-created chat message onto every member's WS channel
    + bump their inbox row. Same shape the regular chat send path uses,
    so the FE handlers (`message.new`, `inbox.bump`) light up identically.
    """
    try:
        # Imports kept local to keep agent.py's import surface small.
        from app.chat_layer import (
            redis_chat, store as _store, user_info_cache,
        )
        from app.chat_layer.api.messages_api import _to_message_out
    except Exception as exc:  # pragma: no cover
        logger.warning("broadcast helper import failed: %s", exc)
        return
    try:
        members = _store.member_user_ids(db, conversation_id)
        payload = _to_message_out(msg, attachment=None, mention_ids=[], db=db)
        sender_info = user_info_cache.get_user_info(msg.sender_id, db=db)
        preview = {
            "id": msg.id,
            "sender_id": msg.sender_id,
            "sender_username": sender_info.get("username"),
            "sender_name": sender_info.get("name"),
            "message_type": msg.message_type,
            "body_preview": _store._preview_for(
                msg.message_type, msg.body, msg.deleted_at,
            ),
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "deleted_at": None,
        }
        for uid in members:
            redis_chat.publish_message_new(
                user_id=uid, message=payload, conversation_id=conversation_id,
            )
            unread = _store.unread_count_for_user(db, conversation_id, uid)
            redis_chat.publish_inbox_bump(
                user_id=uid, conversation_id=conversation_id,
                latest_message=preview, unread_count=unread,
            )
    except Exception as exc:
        logger.warning("ai message broadcast failed: %s", exc, exc_info=True)


def _post_user_message(
    db: Session,
    conversation_id: int,
    user_id: int,
    body: str,
    refs: Optional[List[Dict[str, Any]]],
) -> Optional[int]:
    """Persist the caller's outgoing prompt as a real chat message and push
    it on the WS. Without this the user never sees their own typed text in
    the AI thread (the agent only writes the reply)."""
    try:
        msg = chat_store.create_message(
            db,
            conversation_id=conversation_id,
            sender_id=user_id,
            message_type="text",
            body=body,
            refs=(refs or None),
            is_system=False,
        )
        _broadcast_message(db, msg, conversation_id)
        return msg.id
    except Exception as exc:
        logger.warning("user prompt persist failed: %s", exc, exc_info=True)
        return None


def _post_reply(
    db: Session,
    conversation_id: int,
    user_id: int,
    text: str,
    refs: List[Dict[str, Any]],
    artifacts: List[Dict[str, Any]],
) -> Optional[int]:
    """Insert the AI reply as a chat message from the AI bot user. Best-effort
    — logs and returns None on failure so the caller still gets a result."""
    try:
        bot_id = system_bot.ensure_ai_bot_user(db)
        # We embed artifacts inline as a JSON tail of the refs list with
        # type="ai_artifact" so the FE can pluck them out without a new
        # column. The message body still carries the human text.
        all_refs = list(refs)
        for art in artifacts:
            all_refs.append({
                "type": "ai_artifact",
                "id": art.get("s3_key"),
                "params": {
                    "kind": art.get("kind"),
                    "url": art.get("url"),
                    "mime": art.get("mime"),
                    "file_name": art.get("meta", {}).get("file_name"),
                    "title": art.get("meta", {}).get("title"),
                },
            })
        msg = chat_store.create_message(
            db,
            conversation_id=conversation_id,
            sender_id=bot_id,
            message_type="text",
            body=text,
            refs=all_refs or None,
            is_system=True,
        )
        _broadcast_message(db, msg, conversation_id)
        return msg.id
    except Exception as exc:
        logger.exception("AI reply persist failed: %s", exc)
        return None
