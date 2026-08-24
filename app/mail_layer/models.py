"""Mail SQLAlchemy ORM models. Mirrors migrations/v24_mail_core_tables.sql."""
import logging

from sqlalchemy import (
    BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.mysql import BLOB, JSON, MEDIUMTEXT, TINYINT
from sqlalchemy.orm import relationship

from app.database_Layer.db_config import Base

# Load the cross-service user/team models into the shared Base.metadata so the
# FK to users(id) resolves at flush time (same trick chat/models.py uses).
import app.database_Layer.db_model  # noqa: F401

logger = logging.getLogger("app_logger")


class MailAccount(Base):
    __tablename__ = "mail_accounts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    organization_id = Column(Integer, nullable=True, index=True)
    display_name = Column(String(255), nullable=False)
    email_address = Column(String(320), nullable=False)
    provider = Column(String(20), nullable=False, server_default="custom")

    smtp_host = Column(String(255), nullable=False)
    smtp_port = Column(Integer, nullable=False, server_default="587")
    smtp_security = Column(String(10), nullable=False, server_default="starttls")
    smtp_username = Column(String(320), nullable=False)
    smtp_password_enc = Column(BLOB, nullable=False)

    imap_host = Column(String(255), nullable=False)
    imap_port = Column(Integer, nullable=False, server_default="993")
    imap_security = Column(String(10), nullable=False, server_default="ssl")
    imap_username = Column(String(320), nullable=False)
    imap_password_enc = Column(BLOB, nullable=True)
    use_same_credentials = Column(TINYINT(1), nullable=False, server_default="1")

    sync_enabled = Column(TINYINT(1), nullable=False, server_default="1")
    sync_interval_seconds = Column(Integer, nullable=False, server_default="300")
    backfill_days = Column(Integer, nullable=False, server_default="90")
    use_idle = Column(TINYINT(1), nullable=False, server_default="0")

    status = Column(String(20), nullable=False, server_default="pending")
    last_error = Column(Text, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    consent_acknowledged = Column(TINYINT(1), nullable=False, server_default="0")
    is_default = Column(TINYINT(1), nullable=False, server_default="1")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(),
                        onupdate=func.now())
    deleted_at = Column(DateTime, nullable=True)

    folders = relationship("MailFolder", back_populates="account",
                           cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("user_id", "email_address", name="uq_user_email"),
        Index("idx_mail_acct_user", "user_id"),
        Index("idx_mail_acct_sync", "status", "sync_enabled", "last_synced_at"),
    )


class MailFolder(Base):
    __tablename__ = "mail_folders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("mail_accounts.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, nullable=False)
    organization_id = Column(Integer, nullable=True)
    name = Column(String(255), nullable=False)
    role = Column(String(12), nullable=False, server_default="custom")
    imap_path = Column(String(512), nullable=True)
    parent_folder_id = Column(Integer, ForeignKey("mail_folders.id"), nullable=True)
    uidvalidity = Column(BigInteger, nullable=True)
    last_uid = Column(BigInteger, nullable=True)
    unread_count = Column(Integer, nullable=False, server_default="0")
    total_count = Column(Integer, nullable=False, server_default="0")
    sort_order = Column(Integer, nullable=False, server_default="0")
    is_system = Column(TINYINT(1), nullable=False, server_default="0")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    deleted_at = Column(DateTime, nullable=True)

    account = relationship("MailAccount", back_populates="folders")

    __table_args__ = (
        UniqueConstraint("account_id", "role", "is_system", name="uq_acct_role_sys"),
        Index("idx_mail_fold_acct", "account_id"),
        Index("idx_mail_fold_user", "user_id"),
    )


class MailMessage(Base):
    __tablename__ = "mail_messages"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("mail_accounts.id", ondelete="CASCADE"),
                        nullable=False)
    folder_id = Column(Integer, ForeignKey("mail_folders.id"), nullable=False)
    user_id = Column(Integer, nullable=False)
    organization_id = Column(Integer, nullable=True)
    imap_uid = Column(BigInteger, nullable=True)
    message_id_hdr = Column(String(512), nullable=True)
    conversation_id = Column(String(255), nullable=True)
    in_reply_to = Column(String(512), nullable=True)
    references_hdr = Column(Text, nullable=True)
    direction = Column(String(3), nullable=False)
    from_name = Column(String(255), nullable=True)
    from_address = Column(String(320), nullable=True)
    to_json = Column(JSON, nullable=True)
    cc_json = Column(JSON, nullable=True)
    bcc_json = Column(JSON, nullable=True)
    reply_to_address = Column(String(320), nullable=True)
    subject = Column(String(998), nullable=True)
    snippet = Column(String(512), nullable=True)
    body_text = Column(MEDIUMTEXT, nullable=True)
    body_html = Column(MEDIUMTEXT, nullable=True)
    has_attachments = Column(TINYINT(1), nullable=False, server_default="0")
    size_bytes = Column(Integer, nullable=False, server_default="0")
    is_read = Column(TINYINT(1), nullable=False, server_default="0")
    is_flagged = Column(TINYINT(1), nullable=False, server_default="0")
    is_draft = Column(TINYINT(1), nullable=False, server_default="0")
    is_answered = Column(TINYINT(1), nullable=False, server_default="0")
    is_forwarded = Column(TINYINT(1), nullable=False, server_default="0")
    importance = Column(String(6), nullable=False, server_default="normal")
    send_status = Column(String(10), nullable=True)
    send_attempts = Column(Integer, nullable=False, server_default="0")
    send_error = Column(Text, nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    received_at = Column(DateTime, nullable=True)
    internal_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(),
                        onupdate=func.now())
    deleted_at = Column(DateTime, nullable=True)

    attachments = relationship("MailAttachment", back_populates="message",
                               cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("account_id", "folder_id", "imap_uid", name="uq_acct_folder_uid"),
        Index("idx_mail_msg_folder_date", "folder_id", "internal_date"),
        Index("idx_mail_msg_user_unread", "user_id", "is_read"),
        Index("idx_mail_msg_conv", "account_id", "conversation_id"),
        Index("idx_mail_msg_msgid", "account_id", "message_id_hdr"),
        Index("idx_mail_msg_send", "send_status", "scheduled_at"),
    )


class MailAttachment(Base):
    __tablename__ = "mail_attachments"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("mail_messages.id", ondelete="CASCADE"),
                        nullable=False)
    account_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    s3_key = Column(String(512), nullable=True)
    content_id = Column(String(255), nullable=True)
    file_name = Column(String(255), nullable=False)
    mime_type = Column(String(120), nullable=False)
    size_bytes = Column(BigInteger, nullable=False, server_default="0")
    is_inline = Column(TINYINT(1), nullable=False, server_default="0")
    fetched = Column(TINYINT(1), nullable=False, server_default="0")
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    message = relationship("MailMessage", back_populates="attachments")

    __table_args__ = (Index("idx_mail_att_msg", "message_id"),)


class MailSettings(Base):
    __tablename__ = "mail_settings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("mail_accounts.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, nullable=False)
    organization_id = Column(Integer, nullable=True)
    focused_inbox = Column(TINYINT(1), nullable=False, server_default="0")
    conversation_view = Column(TINYINT(1), nullable=False, server_default="1")
    reading_pane = Column(String(6), nullable=False, server_default="right")
    preview_lines = Column(TINYINT, nullable=False, server_default="2")
    mark_read_behavior = Column(String(20), nullable=False, server_default="on_selection")
    mark_read_delay_seconds = Column(Integer, nullable=False, server_default="0")
    external_images = Column(String(6), nullable=False, server_default="block")
    compose_format = Column(String(4), nullable=False, server_default="html")
    default_font = Column(String(60), nullable=False, server_default="Calibri")
    default_font_size = Column(Integer, nullable=False, server_default="11")
    undo_send_seconds = Column(Integer, nullable=False, server_default="10")
    request_read_receipt = Column(TINYINT(1), nullable=False, server_default="0")
    request_delivery_receipt = Column(TINYINT(1), nullable=False, server_default="0")
    read_receipt_response = Column(String(6), nullable=False, server_default="ask")
    auto_reply_enabled = Column(TINYINT(1), nullable=False, server_default="0")
    auto_reply_subject = Column(String(255), nullable=True)
    auto_reply_message_html = Column(MEDIUMTEXT, nullable=True)
    auto_reply_start = Column(DateTime, nullable=True)
    auto_reply_end = Column(DateTime, nullable=True)
    auto_reply_internal_only = Column(TINYINT(1), nullable=False, server_default="0")
    forwarding_enabled = Column(TINYINT(1), nullable=False, server_default="0")
    forwarding_address = Column(String(320), nullable=True)
    forwarding_keep_copy = Column(TINYINT(1), nullable=False, server_default="1")
    empty_trash_on_exit = Column(TINYINT(1), nullable=False, server_default="0")
    prefs_json = Column(JSON, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(),
                        onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("account_id", name="uq_mail_settings_acct"),
    )


class MailSignature(Base):
    __tablename__ = "mail_signatures"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("mail_accounts.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, nullable=False)
    name = Column(String(120), nullable=False)
    body_html = Column(MEDIUMTEXT, nullable=False)
    is_default_new = Column(TINYINT(1), nullable=False, server_default="0")
    is_default_reply = Column(TINYINT(1), nullable=False, server_default="0")
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (Index("idx_mail_sig_acct", "account_id"),)


class MailRule(Base):
    __tablename__ = "mail_rules"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("mail_accounts.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, nullable=False)
    name = Column(String(150), nullable=False)
    is_enabled = Column(TINYINT(1), nullable=False, server_default="1")
    priority = Column(Integer, nullable=False, server_default="0")
    conditions_json = Column(JSON, nullable=False)
    actions_json = Column(JSON, nullable=False)
    stop_processing = Column(TINYINT(1), nullable=False, server_default="0")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(),
                        onupdate=func.now())

    __table_args__ = (Index("idx_mail_rule_acct", "account_id", "priority"),)


class MailSenderList(Base):
    __tablename__ = "mail_sender_lists"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("mail_accounts.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, nullable=False)
    list_type = Column(String(8), nullable=False)  # blocked | safe
    address_or_domain = Column(String(320), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("account_id", "list_type", "address_or_domain", name="uq_sender"),
    )


class MailCategory(Base):
    __tablename__ = "mail_categories"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("mail_accounts.id", ondelete="CASCADE"),
                        nullable=False)
    user_id = Column(Integer, nullable=False)
    name = Column(String(80), nullable=False)
    color = Column(String(20), nullable=False, server_default="blue")
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("account_id", "name", name="uq_cat"),)


class MailMessageCategory(Base):
    __tablename__ = "mail_message_categories"
    message_id = Column(BigInteger, ForeignKey("mail_messages.id", ondelete="CASCADE"),
                        primary_key=True)
    category_id = Column(Integer, ForeignKey("mail_categories.id", ondelete="CASCADE"),
                         primary_key=True)
