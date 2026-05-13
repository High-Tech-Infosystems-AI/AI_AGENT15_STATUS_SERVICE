"""Chat SQLAlchemy ORM models. Mirrors migrations v8-v10."""
import logging
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, DateTime, ForeignKey,
    Index, UniqueConstraint, func,
)
from sqlalchemy.dialects.mysql import JSON, TINYINT
from sqlalchemy.orm import relationship
from app.database_Layer.db_config import Base

# Ensure cross-table FKs (users, teams) can resolve at flush time.
# These models are owned by other services but live in the same DB and the
# same SQLAlchemy Base.metadata; importing them here loads them into metadata.
import app.database_Layer.db_model  # noqa: F401

logger = logging.getLogger("app_logger")


class ChatConversation(Base):
    __tablename__ = "chat_conversations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(10), nullable=False)  # dm | team | general
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    title = Column(String(255), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    last_message_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)

    members = relationship("ChatConversationMember", back_populates="conversation",
                           cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("team_id", name="uq_team_conv"),
        Index("idx_last_message_desc", "last_message_at"),
    )


class ChatConversationMember(Base):
    __tablename__ = "chat_conversation_members"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id", ondelete="CASCADE"),
                             nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role_in_conversation = Column(String(20), nullable=False, server_default="member")
    joined_at = Column(DateTime, nullable=False, server_default=func.now())
    last_read_message_id = Column(BigInteger, nullable=True)
    last_read_at = Column(DateTime, nullable=True)
    muted = Column(TINYINT(1), nullable=False, server_default="0")
    archived = Column(TINYINT(1), nullable=False, server_default="0")

    conversation = relationship("ChatConversation", back_populates="members")

    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id", name="uq_conv_user"),
        Index("idx_user_conv", "user_id", "conversation_id"),
    )


class ChatMessageAttachment(Base):
    __tablename__ = "chat_message_attachments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    s3_key = Column(String(512), nullable=False)
    mime_type = Column(String(100), nullable=False)
    file_name = Column(String(255), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    duration_seconds = Column(Integer, nullable=True)
    waveform_json = Column(Text, nullable=True)
    thumbnail_s3_key = Column(String(512), nullable=True)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    message_type = Column(String(10), nullable=False, server_default="text")
    # Synthetic system messages (Status Bot replies, etc.) are flagged so the
    # renderer can give them a distinct identity.
    is_system = Column(TINYINT(1), nullable=False, server_default="0")
    body = Column(Text, nullable=True)
    # Structured entity references — list of {type, id} dicts. The body holds
    # opaque @@ref:type:id@@ tokens at the matching positions.
    refs = Column(JSON, nullable=True)
    attachment_id = Column(Integer, ForeignKey("chat_message_attachments.id"), nullable=True)
    reply_to_message_id = Column(BigInteger, ForeignKey("chat_messages.id"), nullable=True)
    forwarded_from_message_id = Column(BigInteger, ForeignKey("chat_messages.id"), nullable=True)
    forwarded_from_sender_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    edited_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    deleted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_conv_created", "conversation_id", "created_at"),
        Index("idx_sender", "sender_id"),
    )


class ChatMessageMention(Base):
    __tablename__ = "chat_message_mentions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("chat_messages.id", ondelete="CASCADE"),
                        nullable=False)
    mentioned_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    __table_args__ = (
        UniqueConstraint("message_id", "mentioned_user_id", name="uq_msg_user_mention"),
        Index("idx_mention_user", "mentioned_user_id"),
    )


class ChatMessageEdit(Base):
    __tablename__ = "chat_message_edits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("chat_messages.id", ondelete="CASCADE"),
                        nullable=False)
    previous_body = Column(Text, nullable=False)
    edited_at = Column(DateTime, nullable=False, server_default=func.now())


class ChatMessageRead(Base):
    __tablename__ = "chat_message_reads"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("chat_messages.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    read_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_msg_user_read"),
    )


class ChatMessageDelivery(Base):
    __tablename__ = "chat_message_deliveries"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("chat_messages.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    delivered_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_msg_user_delivery"),
    )


class ChatMessageReaction(Base):
    __tablename__ = "chat_message_reactions"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("chat_messages.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    emoji = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", "emoji", name="uq_msg_user_emoji"),
        Index("idx_react_message_id", "message_id"),
        Index("idx_react_user", "user_id"),
    )


class ChatUserPresence(Base):
    __tablename__ = "chat_user_presence"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    status = Column(String(10), nullable=False, server_default="offline")
    last_seen_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ChatPushSubscription(Base):
    """One row per (user, browser/device) Web Push subscription. Endpoint is
    canonical; we also store its sha256 as the unique key so the index stays
    short (FCM URLs can exceed 700 chars)."""
    __tablename__ = "chat_push_subscriptions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    endpoint = Column(String(2048), nullable=False)
    endpoint_hash = Column(String(64), nullable=False)
    p256dh = Column(String(255), nullable=False)
    auth_secret = Column(String(255), nullable=False)
    user_agent = Column(String(512), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("endpoint_hash", name="uq_chat_push_endpoint"),
        Index("idx_chat_push_user", "user_id"),
    )


# ---------------------------------------------------------------------------
# Polls + Tasks (migration v20)
# ---------------------------------------------------------------------------

from sqlalchemy import Enum  # noqa: E402  (kept local — import once)


class ChatPoll(Base):
    """A poll attached 1:1 to a chat_messages row. The owning message
    has message_type='poll'; voting / closing happens through the
    auxiliary tables here."""
    __tablename__ = "chat_polls"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("chat_messages.id"),
                         nullable=False, unique=True)
    question = Column(String(500), nullable=False)
    allow_multiple = Column(TINYINT(1), nullable=False, server_default="0")
    closed_at = Column(DateTime, nullable=True)
    closed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class ChatPollOption(Base):
    __tablename__ = "chat_poll_options"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    poll_id = Column(BigInteger, ForeignKey("chat_polls.id", ondelete="CASCADE"),
                      nullable=False)
    text = Column(String(255), nullable=False)
    position = Column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        Index("idx_option_poll", "poll_id", "position"),
    )


class ChatPollVote(Base):
    __tablename__ = "chat_poll_votes"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    poll_id = Column(BigInteger, ForeignKey("chat_polls.id", ondelete="CASCADE"),
                      nullable=False)
    option_id = Column(BigInteger, ForeignKey("chat_poll_options.id",
                                                ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    voted_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("poll_id", "option_id", "user_id",
                         name="uq_poll_user_option"),
        Index("idx_vote_poll_user", "poll_id", "user_id"),
    )


class ChatTask(Base):
    """A task attached to a chat_messages row. ONE message can carry
    MANY tasks (the multi-task / "task list" composer creates a single
    message with several chat_tasks rows). Each row has its own
    assignees / due / priority. Migration v21 dropped the original
    UNIQUE on message_id.
    """
    __tablename__ = "chat_tasks"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("chat_messages.id"),
                         nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    due_at = Column(DateTime, nullable=True)
    priority = Column(Enum("low", "medium", "high",
                            name="chat_task_priority"),
                       nullable=False, server_default="medium")
    status = Column(Enum("open", "in_progress", "done", "cancelled",
                          name="chat_task_status"),
                     nullable=False, server_default="open")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    completed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_task_status", "status"),
        Index("idx_task_due", "due_at"),
        Index("idx_task_message", "message_id"),
    )


class ChatTaskAssignee(Base):
    __tablename__ = "chat_task_assignees"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(BigInteger, ForeignKey("chat_tasks.id", ondelete="CASCADE"),
                      nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(Enum("open", "done", name="chat_task_assignee_status"),
                     nullable=False, server_default="open")
    assigned_at = Column(DateTime, nullable=False, server_default=func.now())
    assigned_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("task_id", "user_id", name="uq_task_user"),
        Index("idx_assignee_user_status", "user_id", "status"),
    )
