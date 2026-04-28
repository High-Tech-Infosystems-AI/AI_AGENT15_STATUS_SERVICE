"""Chat DB store — pure SQLAlchemy operations, no HTTP/Redis side effects."""
import base64
import json
from datetime import datetime
from typing import Iterable, List, Optional

from sqlalchemy import Integer, and_, exists, func, select
from sqlalchemy.orm import Session

from app.chat_layer.models import (
    ChatConversation,
    ChatConversationMember,
    ChatMessage,
    ChatMessageAttachment,
    ChatMessageDelivery,
    ChatMessageEdit,
    ChatMessageMention,
    ChatMessageRead,
    ChatMessageReaction,
    ChatUserPresence,
)


# ---------- Conversations ----------

def get_or_create_dm(db: Session, user_a_id: int, user_b_id: int):
    """Returns (conversation, newly_added_user_ids).
    `newly_added_user_ids` is `[a, b]` only when the DM is freshly created;
    `[]` when an existing DM is returned. Used by the API layer to decide
    whether to fire presence-announcement events between the two users.
    """
    if user_a_id == user_b_id:
        raise ValueError("DM peers must be different users")
    a, b = sorted([user_a_id, user_b_id])

    stmt = (
        select(ChatConversation.id)
        .join(ChatConversationMember,
              ChatConversationMember.conversation_id == ChatConversation.id)
        .where(ChatConversation.type == "dm")
        .group_by(ChatConversation.id)
        .having(func.count(ChatConversationMember.user_id) == 2)
        .having(func.sum(
            (ChatConversationMember.user_id == a).cast(Integer)
        ) == 1)
        .having(func.sum(
            (ChatConversationMember.user_id == b).cast(Integer)
        ) == 1)
    )
    existing = db.execute(stmt).scalar_one_or_none()
    if existing:
        return db.get(ChatConversation, existing), []

    conv = ChatConversation(type="dm", created_by=a)
    db.add(conv)
    db.flush()
    db.add_all([
        ChatConversationMember(conversation_id=conv.id, user_id=a),
        ChatConversationMember(conversation_id=conv.id, user_id=b),
    ])
    db.commit()
    db.refresh(conv)
    return conv, [a, b]


def get_or_create_team_conversation(db: Session, team_id: int,
                                    member_user_ids: Iterable[int],
                                    created_by: Optional[int]):
    """Returns (conversation, newly_added_user_ids).
    `newly_added_user_ids` is the set of users that this call added to the
    member list — empty if every requested user was already a member.
    """
    member_user_ids = list(member_user_ids)
    conv = db.execute(
        select(ChatConversation).where(ChatConversation.team_id == team_id)
    ).scalar_one_or_none()
    if conv:
        existing_ids = {m.user_id for m in conv.members}
        newly_added = []
        for uid in member_user_ids:
            if uid not in existing_ids:
                db.add(ChatConversationMember(conversation_id=conv.id, user_id=uid))
                newly_added.append(uid)
        if newly_added:
            db.commit()
            db.refresh(conv)
        return conv, newly_added

    conv = ChatConversation(type="team", team_id=team_id, created_by=created_by)
    db.add(conv)
    db.flush()
    for uid in member_user_ids:
        db.add(ChatConversationMember(conversation_id=conv.id, user_id=uid))
    db.commit()
    db.refresh(conv)
    return conv, list(member_user_ids)


def get_general_conversation(db: Session) -> ChatConversation:
    conv = db.execute(
        select(ChatConversation).where(ChatConversation.type == "general")
    ).scalar_one_or_none()
    if not conv:
        conv = ChatConversation(id=1, type="general", title="#general")
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return conv


def ensure_general_member(db: Session, user_id: int) -> None:
    exists_q = db.execute(
        select(ChatConversationMember.id)
        .join(ChatConversation, ChatConversation.id == ChatConversationMember.conversation_id)
        .where(and_(ChatConversation.type == "general",
                    ChatConversationMember.user_id == user_id))
    ).first()
    if exists_q:
        return
    conv = get_general_conversation(db)
    db.add(ChatConversationMember(conversation_id=conv.id, user_id=user_id))
    db.commit()


def list_user_conversations(db: Session, user_id: int) -> List[ChatConversation]:
    stmt = (
        select(ChatConversation)
        .join(ChatConversationMember,
              ChatConversationMember.conversation_id == ChatConversation.id)
        .where(and_(ChatConversationMember.user_id == user_id,
                    ChatConversation.deleted_at.is_(None)))
        .order_by(ChatConversation.last_message_at.desc().nullslast(),
                  ChatConversation.id.desc())
    )
    return list(db.execute(stmt).scalars().all())


def is_member(db: Session, conversation_id: int, user_id: int) -> bool:
    stmt = select(exists().where(and_(
        ChatConversationMember.conversation_id == conversation_id,
        ChatConversationMember.user_id == user_id,
    )))
    return bool(db.execute(stmt).scalar())


def member_user_ids(db: Session, conversation_id: int) -> List[int]:
    rows = db.execute(
        select(ChatConversationMember.user_id)
        .where(ChatConversationMember.conversation_id == conversation_id)
    ).all()
    return [r[0] for r in rows]


def inbox_row_for(db: Session, user_id: int, conversation_id: int) -> Optional[dict]:
    """Return a single enriched inbox row for the given conversation, or
    None if the user isn't a member / the conv is deleted. Same shape as
    one item from `inbox_for_user` so endpoints can return consistent data."""
    rows = inbox_for_user(db, user_id)
    for r in rows:
        if r["id"] == conversation_id:
            return r
    return None


def inbox_for_user(db: Session, user_id: int) -> List[dict]:
    """
    Return the WhatsApp-style inbox: every conversation the user belongs to,
    enriched with the latest message preview, unread count, and peer/team info.

    Output rows have shape:
      {
        "id", "type", "team_id", "title", "last_message_at",
        "unread_count": int,
        "members": [user_id, ...],
        "latest_message": {id, sender_id, message_type, body_preview,
                           created_at, deleted_at} | None,
        "peer": {id, name, username,
                 profile_image_key, profile_image_url} | None,   # DM only
        "team": {id, name} | None,                               # team only
      }

    `profile_image_url` is a short-lived presigned GET URL for the peer's
    avatar in the profiles S3 bucket. Clients should render this directly
    rather than constructing a URL from the raw key. May be None when the
    user has no avatar or S3/profiles bucket isn't configured.

    Uses raw SQL with correlated subqueries to keep the round-trips bounded
    (one query for the headers, one for memberships, one for previews).
    Sorted by last_message_at DESC NULLS LAST, then id DESC — same as WhatsApp.
    """
    from sqlalchemy import text as _text

    # 1. Conversation headers + unread count + latest message id
    headers = db.execute(_text("""
        SELECT c.id, c.type, c.team_id, c.title, c.last_message_at,
               m.last_read_message_id,
               (SELECT COUNT(*)
                  FROM chat_messages cm
                 WHERE cm.conversation_id = c.id
                   AND cm.deleted_at IS NULL
                   AND cm.sender_id <> :uid
                   AND (m.last_read_message_id IS NULL
                        OR cm.id > m.last_read_message_id)
               ) AS unread_count,
               (SELECT MAX(cm2.id) FROM chat_messages cm2
                 WHERE cm2.conversation_id = c.id) AS latest_msg_id
          FROM chat_conversations c
          JOIN chat_conversation_members m ON m.conversation_id = c.id
         WHERE m.user_id = :uid
           AND c.deleted_at IS NULL
         ORDER BY (c.last_message_at IS NULL), c.last_message_at DESC, c.id DESC
    """), {"uid": user_id}).all()

    if not headers:
        return []

    conv_ids = [r._mapping["id"] for r in headers]
    latest_msg_ids = [r._mapping["latest_msg_id"] for r in headers
                      if r._mapping["latest_msg_id"] is not None]

    # 2. All members for these conversations (for the response shape)
    member_rows = db.execute(_text("""
        SELECT conversation_id, user_id
          FROM chat_conversation_members
         WHERE conversation_id IN :ids
    """).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
        {"ids": conv_ids},
    ).all()
    members_by_conv: dict = {}
    for r in member_rows:
        members_by_conv.setdefault(r._mapping["conversation_id"], []).append(
            r._mapping["user_id"]
        )

    # 3. Latest messages (preview + sender)
    latest_by_id: dict = {}
    if latest_msg_ids:
        msg_rows = db.execute(_text("""
            SELECT id, conversation_id, sender_id, message_type, body,
                   created_at, deleted_at
              FROM chat_messages
             WHERE id IN :ids
        """).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": latest_msg_ids},
        ).all()
        for r in msg_rows:
            m = r._mapping
            preview = _preview_for(m["message_type"], m["body"], m["deleted_at"])
            latest_by_id[m["id"]] = {
                "id": m["id"],
                "sender_id": m["sender_id"],
                "message_type": m["message_type"],
                "body_preview": preview,
                "created_at": m["created_at"],
                "deleted_at": m["deleted_at"],
            }

    # 4. Peer info for DMs
    dm_peer_ids = []
    for r in headers:
        if r._mapping["type"] == "dm":
            for uid in members_by_conv.get(r._mapping["id"], []):
                if uid != user_id:
                    dm_peer_ids.append(uid)
                    break
    peer_by_id: dict = {}
    if dm_peer_ids:
        peer_rows = db.execute(_text("""
            SELECT id, name, username, profile_image_key
              FROM users
             WHERE id IN :ids
        """).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": list(set(dm_peer_ids))},
        ).all()
        from app.chat_layer.s3_chat_service import presign_profile_image
        for r in peer_rows:
            m = r._mapping
            key = m.get("profile_image_key")
            peer_by_id[m["id"]] = {
                "id": m["id"],
                "name": m["name"],
                "username": m["username"],
                "profile_image_key": key,
                "profile_image_url": presign_profile_image(key),
            }

    # 5. Team info for team conversations
    team_ids = [r._mapping["team_id"] for r in headers
                if r._mapping["type"] == "team" and r._mapping["team_id"]]
    team_by_id: dict = {}
    if team_ids:
        team_rows = db.execute(_text("""
            SELECT id, name FROM teams WHERE id IN :ids
        """).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": team_ids},
        ).all()
        for r in team_rows:
            team_by_id[r._mapping["id"]] = {
                "id": r._mapping["id"],
                "name": r._mapping["name"],
            }

    # 6. Assemble
    out = []
    for r in headers:
        h = r._mapping
        peer = None
        if h["type"] == "dm":
            for uid in members_by_conv.get(h["id"], []):
                if uid != user_id:
                    peer = peer_by_id.get(uid)
                    break
        team = team_by_id.get(h["team_id"]) if h["type"] == "team" else None
        out.append({
            "id": h["id"],
            "type": h["type"],
            "team_id": h["team_id"],
            "title": h["title"],
            "last_message_at": h["last_message_at"],
            "unread_count": int(h["unread_count"] or 0),
            "members": members_by_conv.get(h["id"], []),
            "latest_message": latest_by_id.get(h["latest_msg_id"]),
            "peer": peer,
            "team": team,
        })
    return out


def _preview_for(message_type: str, body, deleted_at) -> str:
    if deleted_at is not None:
        return "[message deleted]"
    if message_type == "text":
        return ((body or "").strip()[:140]) or ""
    return {
        "image": "[image]",
        "voice": "[voice note]",
        "file": "[file]",
        "system": (body or "")[:140],
    }.get(message_type, "[message]")


def update_last_read(db: Session, *, conversation_id: int, user_id: int,
                     message_id: int) -> None:
    """Bump the member's last_read_message_id forward (never backwards)."""
    from sqlalchemy import update as _update
    db.execute(
        _update(ChatConversationMember)
        .where(and_(
            ChatConversationMember.conversation_id == conversation_id,
            ChatConversationMember.user_id == user_id,
            (ChatConversationMember.last_read_message_id.is_(None)) |
            (ChatConversationMember.last_read_message_id < message_id),
        ))
        .values(last_read_message_id=message_id, last_read_at=datetime.utcnow())
    )
    db.commit()


def unread_count_for_user(db: Session, conversation_id: int, user_id: int) -> int:
    from sqlalchemy import text as _text
    row = db.execute(_text("""
        SELECT
          (SELECT COUNT(*) FROM chat_messages cm
            WHERE cm.conversation_id = :cid
              AND cm.deleted_at IS NULL
              AND cm.sender_id <> :uid
              AND (cm.id > COALESCE(
                  (SELECT last_read_message_id FROM chat_conversation_members
                    WHERE conversation_id = :cid AND user_id = :uid), 0)
              )
          ) AS c
    """), {"cid": conversation_id, "uid": user_id}).first()
    return int(row[0] if row else 0)


# ---------- Messages ----------

def create_message(db: Session, *, conversation_id: int, sender_id: int,
                   message_type: str, body: Optional[str] = None,
                   attachment_id: Optional[int] = None,
                   reply_to_message_id: Optional[int] = None,
                   forwarded_from_message_id: Optional[int] = None,
                   forwarded_from_sender_id: Optional[int] = None) -> ChatMessage:
    msg = ChatMessage(
        conversation_id=conversation_id,
        sender_id=sender_id,
        message_type=message_type,
        body=body,
        attachment_id=attachment_id,
        reply_to_message_id=reply_to_message_id,
        forwarded_from_message_id=forwarded_from_message_id,
        forwarded_from_sender_id=forwarded_from_sender_id,
    )
    db.add(msg)
    db.flush()
    conv = db.get(ChatConversation, conversation_id)
    if conv:
        conv.last_message_at = datetime.utcnow()
    db.commit()
    db.refresh(msg)
    return msg


def get_message(db: Session, message_id: int) -> Optional[ChatMessage]:
    return db.get(ChatMessage, message_id)


def soft_delete_message(db: Session, *, message_id: int, deleted_by: int) -> None:
    msg = db.get(ChatMessage, message_id)
    if not msg:
        return
    msg.deleted_at = datetime.utcnow()
    msg.deleted_by = deleted_by
    db.commit()


def edit_message_body(db: Session, *, message_id: int, new_body: str) -> None:
    msg = db.get(ChatMessage, message_id)
    if not msg:
        return
    db.add(ChatMessageEdit(message_id=msg.id, previous_body=msg.body or ""))
    msg.body = new_body
    msg.edited_at = datetime.utcnow()
    db.commit()


def _encode_cursor(created_at: datetime, msg_id: int) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "i": msg_id})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str):
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    obj = json.loads(raw)
    return datetime.fromisoformat(obj["t"]), int(obj["i"])


def list_messages(db: Session, *, conversation_id: int,
                  cursor: Optional[str], limit: int = 50):
    q = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )
    if cursor:
        try:
            ts, mid = _decode_cursor(cursor)
            q = q.where(
                (ChatMessage.created_at < ts) |
                ((ChatMessage.created_at == ts) & (ChatMessage.id < mid))
            )
        except Exception:
            pass
    q = q.limit(limit + 1)
    rows = list(db.execute(q).scalars().all())
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if rows and has_more else None
    return rows, has_more, next_cursor


# ---------- Mentions / reads / deliveries ----------

def add_mentions(db: Session, message_id: int, user_ids: Iterable[int]) -> None:
    for uid in set(user_ids):
        db.add(ChatMessageMention(message_id=message_id, mentioned_user_id=uid))
    db.commit()


def mark_delivered(db: Session, *, message_id: int, user_id: int) -> None:
    if not db.execute(select(ChatMessageDelivery).where(and_(
        ChatMessageDelivery.message_id == message_id,
        ChatMessageDelivery.user_id == user_id,
    ))).scalar_one_or_none():
        db.add(ChatMessageDelivery(message_id=message_id, user_id=user_id))
        db.commit()


def mark_read(db: Session, *, message_id: int, user_id: int) -> None:
    if not db.execute(select(ChatMessageRead).where(and_(
        ChatMessageRead.message_id == message_id,
        ChatMessageRead.user_id == user_id,
    ))).scalar_one_or_none():
        db.add(ChatMessageRead(message_id=message_id, user_id=user_id))
        db.commit()


def read_count(db: Session, message_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(ChatMessageRead).where(
            ChatMessageRead.message_id == message_id
        )
    ).scalar_one()


# ---------- Presence ----------

# ---------- Reactions ----------

def add_reaction(db: Session, *, message_id: int, user_id: int, emoji: str) -> bool:
    """Add a (msg, user, emoji) reaction. Returns True if newly added,
    False if it already existed (idempotent)."""
    existing = db.execute(
        select(ChatMessageReaction).where(and_(
            ChatMessageReaction.message_id == message_id,
            ChatMessageReaction.user_id == user_id,
            ChatMessageReaction.emoji == emoji,
        ))
    ).scalar_one_or_none()
    if existing:
        return False
    db.add(ChatMessageReaction(message_id=message_id, user_id=user_id, emoji=emoji))
    db.commit()
    return True


def remove_reaction(db: Session, *, message_id: int, user_id: int, emoji: str) -> bool:
    """Remove a (msg, user, emoji) reaction. Returns True if a row was
    deleted, False if there was nothing to remove (idempotent)."""
    existing = db.execute(
        select(ChatMessageReaction).where(and_(
            ChatMessageReaction.message_id == message_id,
            ChatMessageReaction.user_id == user_id,
            ChatMessageReaction.emoji == emoji,
        ))
    ).scalar_one_or_none()
    if not existing:
        return False
    db.delete(existing)
    db.commit()
    return True


def list_reactions_for_messages(db: Session, message_ids: List[int]) -> dict:
    """Returns {message_id: [{emoji, user_id, username, name, created_at}, ...]}
    Joined to `users` so we don't need a second lookup. Ordered by
    `created_at ASC` so the chip list is stable."""
    if not message_ids:
        return {}
    from sqlalchemy import bindparam, text as _text
    rows = db.execute(_text("""
        SELECT r.message_id, r.user_id, r.emoji, r.created_at,
               u.username, u.name
          FROM chat_message_reactions r
          JOIN users u ON u.id = r.user_id
         WHERE r.message_id IN :ids
         ORDER BY r.created_at ASC
    """).bindparams(bindparam("ids", expanding=True)),
        {"ids": message_ids}).all()
    out: dict = {}
    for r in rows:
        m = r._mapping
        out.setdefault(m["message_id"], []).append({
            "emoji": m["emoji"],
            "user_id": m["user_id"],
            "username": m["username"],
            "name": m["name"],
            "created_at": m["created_at"],
        })
    return out


def group_reactions_by_emoji(rxn_list) -> List[dict]:
    """Group a flat reaction list (from list_reactions_for_messages) into
    [{emoji, count, users}, ...]. Order of first appearance preserved."""
    if not rxn_list:
        return []
    by_emoji: dict = {}
    for r in rxn_list:
        by_emoji.setdefault(r["emoji"], []).append(r)
    return [
        {
            "emoji": emoji,
            "count": len(items),
            "users": [
                {"user_id": x["user_id"], "username": x["username"], "name": x["name"]}
                for x in items
            ],
        }
        for emoji, items in by_emoji.items()
    ]


# ---------- Presence ----------

def upsert_presence(db: Session, user_id: int, status: str,
                    last_seen_at: Optional[datetime] = None) -> ChatUserPresence:
    row = db.get(ChatUserPresence, user_id)
    if row is None:
        row = ChatUserPresence(user_id=user_id, status=status, last_seen_at=last_seen_at)
        db.add(row)
    else:
        row.status = status
        if last_seen_at is not None:
            row.last_seen_at = last_seen_at
    db.commit()
    db.refresh(row)
    return row
