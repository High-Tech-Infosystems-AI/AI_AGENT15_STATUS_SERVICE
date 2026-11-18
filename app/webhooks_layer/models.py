"""Outgoing webhook endpoints and their delivery log (migrations/v30_outgoing_webhooks.sql)."""
from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, Index, Integer, LargeBinary, String, Text, func

from app.database_Layer.db_config import Base


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False, index=True)
    name = Column(String(150), nullable=False)
    description = Column(String(500), nullable=True)
    url = Column(String(2048), nullable=False)
    # Signing secret, Fernet-encrypted with WHATSAPP_CRED_KEY (whatsapp_layer.crypto).
    secret_enc = Column(LargeBinary, nullable=False)
    secret_last4 = Column(String(4), nullable=False)
    # Event names, or ["*"] for every event in the catalogue.
    events = Column(JSON, nullable=False)
    is_active = Column(Integer, nullable=False, server_default="1")
    disabled_reason = Column(String(255), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, server_default="0")
    last_delivery_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=True, onupdate=func.now())
    deleted_at = Column(DateTime, nullable=True)


class WebhookDelivery(Base):
    """One event sent to one endpoint. status: pending | sending | retrying | succeeded | failed."""
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    endpoint_id = Column(Integer, nullable=False)
    organization_id = Column(Integer, nullable=False)
    event_id = Column(String(64), nullable=False)
    event_name = Column(String(100), nullable=False)
    payload = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, server_default="pending")
    attempts = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="6")
    next_attempt_at = Column(DateTime, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    response_status = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    error = Column(String(500), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    delivered_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_webhook_deliveries_endpoint", "endpoint_id", "created_at"),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
    )
