"""Conversation endpoints."""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

from app.chat_layer import presence as presence_helper, store
from app.chat_layer.auth import current_user
from app.chat_layer.chat_acl import can_post_dm, can_post_team
from app.chat_layer.models import ChatConversation
from app.chat_layer.schemas import (
    ConversationOut, CreateDMRequest, ErrorResponse,
)
from app.database_Layer.db_config import SessionLocal
from app.database_Layer.db_model import User

router = APIRouter()


def _is_super_admin(user: dict) -> bool:
    """Role-name based check. `role_name` is set by the auth service and
    cached on the chat-side token validation. Falls back to False if the
    field is missing (treats the caller as a tier user — safest default).
    """
    return (user.get("role_name") or "").lower() == "super_admin"


def _err(code: str, msg: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error_code": code, "message": msg})


def _is_user_active(db, user_id: int) -> bool:
    u = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    return bool(u and getattr(u, "enable", 1))


def _serialise(conv, members):
    return ConversationOut(
        id=conv.id, type=conv.type, team_id=conv.team_id, title=conv.title,
        last_message_at=conv.last_message_at, members=members,
    ).model_dump(mode="json")


def _fetch_team_member_ids(db, team_id: int) -> List[int]:
    rows = db.execute(
        text("SELECT user_id FROM team_members WHERE team_id = :tid"),
        {"tid": team_id},
    ).all()
    return [r[0] for r in rows]


from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx


@router.post("/conversations/dm", response_model=ConversationOut,
             responses={403: {"model": ErrorResponse}})
def create_dm(req: CreateDMRequest,
              ctx: AuthCtx = Depends(get_auth_ctx),
              user: dict = Depends(current_user)):
    # Chat is default-access — no permission key. Tenant scope still applies:
    # the peer must belong to the same org as the caller (or super_admin path).
    # The store.get_or_create_dm helper should stamp organization_id on new
    # rows; this endpoint just enforces the cross-org block.
    db = SessionLocal()
    try:
        if not _is_user_active(db, req.peer_user_id):
            return _err("CHAT_USER_INACTIVE", "Peer user is not active", 403)
        # Cross-tenant DM block: a tenant user cannot DM a user in another org.
        # Use the User model already imported from db_model at module top.
        # The previous local import pulled in db_store.User, which is a
        # near-duplicate class also mapped to __tablename__ = 'users'
        # against the same `Base`. SQLAlchemy registers two mappers for
        # the same table on that import — raising
        # `InvalidRequestError: Table 'users' is already defined for
        # this MetaData instance` the first time create_dm runs after a
        # cold start. The endpoint then 500s. db_model.User has the
        # exact `id` and `organization_id` columns this branch needs,
        # so it serves the cross-tenant check identically.
        if not ctx.is_super_admin and ctx.organization_id is not None:
            peer = db.query(User).filter(User.id == req.peer_user_id).first()
            if peer and getattr(peer, "organization_id", None) not in (None, ctx.organization_id):
                return _err("CHAT_CROSS_TENANT_DM", "Cannot DM a user in another organisation", 403)
        if not can_post_dm(peer_active=True):
            return _err("CHAT_USER_INACTIVE", "Cannot DM inactive user", 403)
        # Pass the caller's tenant down so the new ChatConversation row gets
        # organization_id stamped. Previously this insert left org NULL on
        # every DM, breaking super_admin's per-org inbox filter and any
        # future per-tenant export. The cross-tenant peer-check above
        # already guarantees both peers share `ctx.organization_id` (admin /
        # tier are blocked from cross-org; super_admin DMing across orgs
        # is intentionally allowed and falls back to the peer's org via
        # the store helper).
        conv, newly_added = store.get_or_create_dm(
            db, user["user_id"], req.peer_user_id,
            organization_id=ctx.organization_id,
        )
        members = store.member_user_ids(db, conv.id)
        # Both users now share this DM. Push current presence to both ends
        # so they see each other's online dot immediately — without this,
        # neither side gets a presence event until the next reconnect.
        if newly_added:
            presence_helper.announce_presence_to(
                db=db, target_user_ids=members, about_user_ids=members,
            )
        return _serialise(conv, members)
    finally:
        db.close()


@router.get("/conversations", response_model=List[ConversationOut])
def list_conversations(
    organization_id: Optional[int] = Query(
        None,
        description=(
            "(super_admin only) narrow the personal inbox to one org. "
            "Membership rules still apply — super_admin only sees "
            "conversations they actually belong to; the filter just "
            "hides ones from other orgs. Ignored for admin / tier "
            "users (their inbox is already implicitly org-scoped)."
        ),
    ),
    user: dict = Depends(current_user),
):
    """Inbox view, membership-based for EVERYONE.

    - Tier / admin / super_admin all use the same membership-based
      inbox. No audit/inspection mode.
    - super_admin can additionally pass `?organization_id=N` to FILTER
      their own multi-org inbox down to one org (useful since super_admin
      can join chats across tenants). The filter never expands their
      reach — only narrows it.
    """
    db = SessionLocal()
    try:
        store.ensure_general_member(db, user["user_id"])
        # Super_admin is an implicit member of EVERY org's #general. The
        # first time they pick org N in the navbar, we lazily insert a
        # membership row so #general (N) appears in their inbox and they
        # can post just like a normal member. No backfill needed — the
        # next picker tick handles it on demand.
        is_super = _is_super_admin(user)
        if is_super and organization_id is not None:
            store.ensure_general_member_for_org(
                db, user["user_id"], organization_id,
            )
        # Only super_admin's filter is honoured. Tier users get their
        # natural inbox (their JWT pins them to their own org anyway,
        # so an extra filter would be a no-op).
        org_filter = organization_id if is_super else None
        return store.inbox_for_user(
            db, user["user_id"], organization_id=org_filter,
        )
    finally:
        db.close()


@router.get("/conversations/general", response_model=ConversationOut)
def get_general(
    organization_id: Optional[int] = Query(
        None,
        description=(
            "(super_admin only) view a specific org's #general. "
            "Membership is required — super_admin only gets it if they "
            "actually belong to that org's #general (e.g. an org admin "
            "added them). Ignored for tier / admin users."
        ),
    ),
    user: dict = Depends(current_user),
):
    """Return the caller's #general conversation, membership-checked.

    Each tenant has its own "all group". Members of org A never see
    traffic from org B.

    super_admin without `?organization_id` → their default-org #general
    (the one their JWT org points to, or the legacy NULL-org row).

    super_admin with `?organization_id=X` → org X's #general IF they're
    a member; otherwise 404. No audit override.
    """
    db = SessionLocal()
    try:
        if _is_super_admin(user) and organization_id is not None:
            # Super_admin is an implicit member of every org's #general.
            # Wire them in lazily (same rule as list_conversations) so
            # the row + posting permission is in place by the time we
            # serialise.
            store.ensure_general_member_for_org(
                db, user["user_id"], organization_id,
            )
            conv = store.get_general_conversation(
                db, organization_id=organization_id,
            )
            return _serialise(conv, store.member_user_ids(db, conv.id))

        store.ensure_general_member(db, user["user_id"])
        org_id = store._resolve_user_org(db, user["user_id"])
        conv = store.get_general_conversation(db, organization_id=org_id)
        return _serialise(conv, store.member_user_ids(db, conv.id))
    finally:
        db.close()


@router.get("/conversations/team/{team_id}", response_model=ConversationOut,
            responses={403: {"model": ErrorResponse}})
def get_team_conversation(team_id: int, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        members = _fetch_team_member_ids(db, team_id)
        is_member_local = user["user_id"] in members
        if not can_post_team(role_name=user.get("role_name"), is_member=is_member_local):
            return _err("CHAT_TEAM_MEMBERSHIP_REQUIRED",
                        "You are not a member of this team", 403)
        conv, newly_added = store.get_or_create_team_conversation(
            db, team_id=team_id, member_user_ids=members,
            created_by=user["user_id"],
        )
        all_members = store.member_user_ids(db, conv.id)
        # Cross-announce only between newly-added members and existing ones.
        # On a brand-new team chat that's everyone × everyone (still small for
        # typical team sizes); on an established chat with one new member it's
        # 2*(N-1) events instead of N*N.
        if newly_added:
            existing = [uid for uid in all_members if uid not in newly_added]
            # Newly-added members learn about everyone (incl. each other).
            presence_helper.announce_presence_to(
                db=db, target_user_ids=newly_added, about_user_ids=all_members,
            )
            # Existing members learn about the new arrivals.
            if existing:
                presence_helper.announce_presence_to(
                    db=db, target_user_ids=existing, about_user_ids=newly_added,
                )
        return _serialise(conv, all_members)
    finally:
        db.close()


@router.get("/conversations/{conversation_id}", response_model=ConversationOut,
            responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def get_conversation(conversation_id: int, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        # Membership is required for EVERYONE, including super_admin.
        # Audit/inspection mode is gone — super_admin only sees what
        # they're a member of, just like a normal user.
        if not store.is_member(db, conversation_id, user["user_id"]):
            return _err("CHAT_NOT_MEMBER", "Not a conversation member", 403)
        conv = db.get(ChatConversation, conversation_id)
        if not conv or conv.deleted_at is not None:
            return _err("CHAT_NOT_FOUND", "Conversation not found", 404)
        # Return the SAME enriched shape the inbox endpoint returns —
        # with peer info (DM), team info, latest_message preview, and
        # unread_count. Without this, the client's per-conversation cache
        # gets overwritten with a stripped-down record on every refetch
        # (header flickers from "Alice" → "Direct Message").
        enriched = store.inbox_row_for(db, user["user_id"], conv.id)
        if enriched is not None:
            return enriched
        # Fallback: caller is technically a member but the inbox query
        # didn't return a row (rare). Serve the basic shape.
        return _serialise(conv, store.member_user_ids(db, conv.id))
    finally:
        db.close()


# ── User lookup (used by the task-assignee picker) ───────────────────

class _UserLookupRequest(BaseModel):
    user_ids: List[int] = Field(..., min_length=1, max_length=200)


class _UserLookupOut(BaseModel):
    id: int
    name: Optional[str] = None
    username: Optional[str] = None


@router.post("/users/lookup", response_model=List[_UserLookupOut])
def lookup_users(body: "_UserLookupRequest",
                  user: dict = Depends(current_user)) -> List["_UserLookupOut"]:
    """Resolve a batch of user_ids to {id, name, username}. Used by
    the task composer's assignee picker (and any other place that
    needs to render a user list from a member_id array). Caller must
    be an authenticated user; visibility is intentionally lax — chat
    members already see each other's names through every other
    surface, and this endpoint never reveals emails / roles.
    """
    if not body.user_ids:
        return []
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT id, name, username FROM users "
                "WHERE id IN :ids AND deleted_at IS NULL",
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": list({int(i) for i in body.user_ids})},
        ).all()
        return [
            _UserLookupOut(
                id=int(r._mapping["id"]),
                name=r._mapping.get("name"),
                username=r._mapping.get("username"),
            )
            for r in rows
        ]
    finally:
        db.close()
