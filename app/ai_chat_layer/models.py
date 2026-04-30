"""SQLAlchemy ORM for the AI chatbot tables (migrations v18, v19)."""
from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Enum, Index, Integer, String, Text,
    func,
)
from sqlalchemy.dialects.mysql import JSON, MEDIUMTEXT, TINYINT

from app.database_Layer.db_config import Base
import app.chat_layer.models  # noqa: F401  ensure shared metadata loaded


class AiTokenQuota(Base):
    __tablename__ = "ai_token_quota"
    user_id = Column(Integer, primary_key=True)
    daily_limit = Column(Integer, nullable=False, server_default="50000")
    monthly_limit = Column(Integer, nullable=False, server_default="1000000")
    used_today = Column(Integer, nullable=False, server_default="0")
    used_month = Column(Integer, nullable=False, server_default="0")
    day_anchor = Column(Date, nullable=False)
    month_anchor = Column(String(7), nullable=False)
    updated_by = Column(Integer, nullable=True)
    updated_at = Column(
        DateTime, nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class AiQueryAudit(Base):
    __tablename__ = "ai_query_audit"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    conversation_id = Column(Integer, nullable=True)
    prompt = Column(MEDIUMTEXT, nullable=False)
    refs = Column(JSON, nullable=True)
    tools_called = Column(JSON, nullable=True)
    model = Column(String(64), nullable=False)
    prompt_version = Column(String(32), nullable=False)
    tokens_in = Column(Integer, nullable=False, server_default="0")
    tokens_out = Column(Integer, nullable=False, server_default="0")
    latency_ms = Column(Integer, nullable=False, server_default="0")
    status = Column(
        Enum("ok", "error", "rejected_quota", "rejected_acl"),
        nullable=False,
    )
    error_msg = Column(String(500), nullable=True)
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_audit_user_time", "user_id", "created_at"),
        Index("idx_audit_status", "status"),
    )


class AiScheduledQuery(Base):
    __tablename__ = "ai_scheduled_query"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    name = Column(String(120), nullable=False)
    prompt = Column(MEDIUMTEXT, nullable=False)
    refs = Column(JSON, nullable=True)
    cron_expr = Column(String(64), nullable=False)
    timezone = Column(String(64), nullable=False, server_default="Asia/Kolkata")
    is_active = Column(TINYINT(1), nullable=False, server_default="0")
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_sq_due", "is_active", "next_run_at"),
        Index("idx_sq_user", "user_id"),
    )


class AiAnomalySubscription(Base):
    __tablename__ = "ai_anomaly_subscription"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    name = Column(String(120), nullable=False)
    metric_key = Column(String(64), nullable=False)
    params = Column(JSON, nullable=False)
    is_active = Column(TINYINT(1), nullable=False, server_default="0")
    cooldown_min = Column(Integer, nullable=False, server_default="360")
    last_fired_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_an_active", "is_active"),
        Index("idx_an_user", "user_id"),
    )


class AiApproval(Base):
    __tablename__ = "ai_approval"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    origin = Column(
        Enum("schedule_create", "anomaly_create", "action_suggest"),
        nullable=False,
    )
    payload = Column(JSON, nullable=False)
    status = Column(
        Enum("pending", "approved", "declined", "expired"),
        nullable=False, server_default="pending",
    )
    approver_role = Column(
        Enum("admin_or_super", "super", "self"), nullable=False,
    )
    decided_by = Column(Integer, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    target_id = Column(BigInteger, nullable=True)
    target_kind = Column(String(32), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_approval_user_status", "user_id", "status"),
        Index("idx_approval_role_status", "approver_role", "status"),
    )


class AiArtifact(Base):
    """Registry of AI-generated S3 artifacts (PDFs, charts, CSVs, markdown).

    Lets `list_artifacts` / `get_artifact_url` recover prior outputs
    after their original presigned URLs have expired.
    """
    __tablename__ = "ai_artifact"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    kind = Column(String(32), nullable=False)
    s3_key = Column(String(512), nullable=False)
    mime = Column(String(80), nullable=False)
    file_name = Column(String(255), nullable=True)
    title = Column(String(200), nullable=True)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_artifact_user_time", "user_id", "created_at"),
        Index("idx_artifact_kind", "kind"),
    )
