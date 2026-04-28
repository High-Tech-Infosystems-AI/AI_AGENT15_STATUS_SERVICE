"""Chat SQLAlchemy ORM models. Mirrors migrations v8-v10."""
import logging
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, DateTime, ForeignKey,
    Index, UniqueConstraint, func,
)
from sqlalchemy.dialects.mysql import TINYINT
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
    body = Column(Text, nullable=True)
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
