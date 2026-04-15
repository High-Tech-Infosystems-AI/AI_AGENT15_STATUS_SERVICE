"""
Notification SQLAlchemy ORM Models

Tables:
- notifications          – core notification record
- notification_recipients – per-user delivery + read tracking
- notification_schedules  – scheduled / recurring notifications
- notification_events     – auto-notification event registry
"""

import logging
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, TIMESTAMP,
    ForeignKey, Index, UniqueConstraint, func,
)
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.orm import relationship
from app.database_Layer.db_config import Base

logger = logging.getLogger("app_logger")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)

    # Delivery: push | banner | log
    # - push   → WS type "notification" (shown in notification feed, counts toward unread)
    # - banner → WS type "banner"       (top-of-page announcement, active banners API)
    # - log    → WS type "log"          (audit trail only, excluded from unread count)
    delivery_mode = Column(String(20), nullable=False)

    # Domain type for filtering: login, jobs, ai, candidate, security, system, user_management
    domain_type = Column(String(30), nullable=False)

    # Visibility: personal, public, restricted
    visibility = Column(String(20), nullable=False)

    # Priority: low, medium, high, critical
    priority = Column(String(20), nullable=False, server_default="medium")

    # Targeting
    target_type = Column(String(20), nullable=False)  # all, user, job, role
    target_id = Column(String(255), nullable=True)     # csv user_ids | job id | role name

    # Source tracking
    source_service = Column(String(50), nullable=True)  # login, job, candidate, resume_analyzer, rbac, bulk_candidate, system
    event_type = Column(String(100), nullable=True)      # auto-notification event name

    # Extra context as JSON string
    extra_metadata = Column("metadata", Text, nullable=True)

    # Audit
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())

    # Banner expiration
    expires_at = Column(DateTime, nullable=True)

    # Soft-delete
    is_active = Column(TINYINT(1), nullable=False, server_default="1")

    # Relationships
    recipients = relationship("NotificationRecipient", back_populates="notification", lazy="dynamic")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        Index("idx_delivery_mode", "delivery_mode"),
        Index("idx_domain_type", "domain_type"),
        Index("idx_visibility", "visibility"),
        Index("idx_priority", "priority"),
        Index("idx_source_service", "source_service"),
        Index("idx_event_type", "event_type"),
        Index("idx_created_at", "created_at"),
        Index("idx_is_active", "is_active"),
        Index("idx_target_type", "target_type"),
        Index("idx_filter_combo", "domain_type", "visibility", "created_at", "is_active"),
    )


class NotificationRecipient(Base):
    __tablename__ = "notification_recipients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    notification_id = Column(Integer, ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_read = Column(TINYINT(1), nullable=False, server_default="0")
    read_at = Column(DateTime, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())

    # Relationships
    notification = relationship("Notification", back_populates="recipients")
    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint("notification_id", "user_id", name="uq_notif_user"),
        Index("idx_user_read", "user_id", "is_read"),
        Index("idx_user_created", "user_id", "created_at"),
        Index("idx_notif_id", "notification_id"),
    )


class NotificationSchedule(Base):
    __tablename__ = "notification_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    delivery_mode = Column(String(20), nullable=False)
    domain_type = Column(String(30), nullable=False)
    visibility = Column(String(20), nullable=False)
    priority = Column(String(20), nullable=False, server_default="medium")
    target_type = Column(String(20), nullable=False)
    target_id = Column(String(255), nullable=True)
    extra_metadata = Column("metadata", Text, nullable=True)

    scheduled_at = Column(DateTime, nullable=False)
    repeat_type = Column(String(20), nullable=False, server_default="once")  # once, daily, weekly
    repeat_until = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, server_default="pending")  # pending, sent, cancelled
    last_sent_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())

    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        Index("idx_status_scheduled", "status", "scheduled_at"),
        Index("idx_schedule_created_by", "created_by"),
    )


class NotificationEvent(Base):
    __tablename__ = "notification_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    default_title_template = Column(String(255), nullable=False)
    default_message_template = Column(Text, nullable=False)

    domain_type = Column(String(30), nullable=False)
    visibility = Column(String(20), nullable=False)
    target_type = Column(String(20), nullable=False)       # role, job, user, all
    target_roles = Column(String(255), nullable=True)       # csv roles (when target_type=role)
    source_service = Column(String(50), nullable=False)
    priority = Column(String(20), nullable=False, server_default="medium")
    # push | banner | log (see Notification.delivery_mode above)
    delivery_mode = Column(String(20), nullable=False, server_default="push")

    # Dual-delivery: when also_banner=1, a second banner notification is created alongside the primary push.
    # If banner_* fields are NULL, the default templates/target are reused for the banner.
    also_banner = Column(TINYINT(1), nullable=False, server_default="0")
    banner_title_template = Column(String(255), nullable=True)
    banner_message_template = Column(Text, nullable=True)
    banner_target_type = Column(String(20), nullable=True)       # role | job | user | all
    banner_target_roles = Column(String(255), nullable=True)
    banner_expires_hours = Column(Integer, nullable=True)        # default handled in event_handler

    is_enabled = Column(TINYINT(1), nullable=False, server_default="1")
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
