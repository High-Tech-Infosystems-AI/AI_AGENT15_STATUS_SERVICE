"""
Pydantic schemas for Notification Service request/response models.
"""

from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums as string literals (kept simple — no Python Enum overhead)
# ---------------------------------------------------------------------------
DELIVERY_MODES = ("push", "banner")
DOMAIN_TYPES = ("login", "jobs", "ai", "candidate", "security", "system", "user_management")
VISIBILITY_LEVELS = ("personal", "public", "restricted")
PRIORITIES = ("low", "medium", "high", "critical")
TARGET_TYPES = ("all", "user", "job", "role")
REPEAT_TYPES = ("once", "daily", "weekly")
SCHEDULE_STATUSES = ("pending", "sent", "cancelled")


# ---------------------------------------------------------------------------
# Shared / Base
# ---------------------------------------------------------------------------
class PaginationDetails(BaseModel):
    page: int
    limit: int
    total_pages: int
    total_elements: int


# ---------------------------------------------------------------------------
# Send Notification (Manual trigger by admin)
# ---------------------------------------------------------------------------
class SendNotificationRequest(BaseModel):
    title: str = Field(..., max_length=255)
    message: str
    delivery_mode: str = Field("push", description="push or banner")
    domain_type: str = Field("system", description="login, jobs, ai, candidate, security, system, user_management")
    visibility: str = Field("public", description="personal, public, restricted")
    priority: str = Field("medium", description="low, medium, high, critical")
    target_type: str = Field("all", description="all, user, job, role")
    target_id: Optional[str] = Field(None, description="csv user_ids, job id, or role name")
    metadata: Optional[dict] = None


class SendNotificationResponse(BaseModel):
    success: bool
    notification_id: int
    recipients_count: int
    message: str


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
class CreateBannerRequest(BaseModel):
    title: str = Field(..., max_length=255)
    message: str
    priority: str = Field("medium")
    domain_type: str = Field("system")
    expires_at: Optional[datetime] = None
    metadata: Optional[dict] = None


class BannerResponse(BaseModel):
    id: int
    title: str
    message: str
    priority: str
    domain_type: str
    expires_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Notification (user-facing read model)
# ---------------------------------------------------------------------------
class NotificationOut(BaseModel):
    id: int
    title: str
    message: str
    delivery_mode: str
    domain_type: str
    visibility: str
    priority: str
    source_service: Optional[str] = None
    event_type: Optional[str] = None
    metadata: Optional[Any] = None
    created_at: datetime
    is_read: bool = False
    read_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    notifications: List[NotificationOut]
    pagination: PaginationDetails
    unread_count: int


# ---------------------------------------------------------------------------
# Admin Log (extended fields)
# ---------------------------------------------------------------------------
class AdminNotificationOut(BaseModel):
    id: int
    title: str
    message: str
    delivery_mode: str
    domain_type: str
    visibility: str
    priority: str
    target_type: str
    target_id: Optional[str] = None
    source_service: Optional[str] = None
    event_type: Optional[str] = None
    metadata: Optional[Any] = None
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None
    is_active: bool = True
    recipients_count: int = 0
    read_count: int = 0

    class Config:
        from_attributes = True


class AdminNotificationListResponse(BaseModel):
    notifications: List[AdminNotificationOut]
    pagination: PaginationDetails


# ---------------------------------------------------------------------------
# Unread Count
# ---------------------------------------------------------------------------
class UnreadCountResponse(BaseModel):
    count: int


# ---------------------------------------------------------------------------
# Read / Mark-all-read
# ---------------------------------------------------------------------------
class MarkReadResponse(BaseModel):
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------
class CreateScheduleRequest(BaseModel):
    title: str = Field(..., max_length=255)
    message: str
    delivery_mode: str = Field("push")
    domain_type: str = Field("system")
    visibility: str = Field("public")
    priority: str = Field("medium")
    target_type: str = Field("all")
    target_id: Optional[str] = None
    metadata: Optional[dict] = None
    scheduled_at: datetime
    repeat_type: str = Field("once")
    repeat_until: Optional[datetime] = None


class ScheduleOut(BaseModel):
    id: int
    title: str
    message: str
    delivery_mode: str
    domain_type: str
    visibility: str
    priority: str
    target_type: str
    target_id: Optional[str] = None
    scheduled_at: datetime
    repeat_type: str
    repeat_until: Optional[datetime] = None
    status: str
    last_sent_at: Optional[datetime] = None
    created_by: int
    created_at: datetime

    class Config:
        from_attributes = True


class ScheduleListResponse(BaseModel):
    schedules: List[ScheduleOut]
    pagination: PaginationDetails


# ---------------------------------------------------------------------------
# Event Trigger (service-to-service)
# ---------------------------------------------------------------------------
class EventTriggerRequest(BaseModel):
    event_name: str = Field(..., max_length=100)
    data: Optional[dict] = Field(default_factory=dict, description="Template variables + context")


class EventTriggerResponse(BaseModel):
    success: bool
    notification_id: Optional[int] = None
    message: str
