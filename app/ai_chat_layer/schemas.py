"""Pydantic request/response schemas for the AI chatbot REST API."""
from datetime import date, datetime
from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from app.chat_layer.schemas import EntityCard, EntityRef


# ---------- Ask / message ----------

class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    refs: List[EntityRef] = Field(default_factory=list, max_length=10)
    # Optional override of the conversation. When omitted, the AI Assistant
    # DM is auto-resolved from the requesting user's identity.
    conversation_id: Optional[int] = None


class AskTaskAck(BaseModel):
    """Returned synchronously from POST /ai-chat/ask. Real reply streams via
    the existing chat WebSocket as `ai.token` events keyed on this task_id."""
    task_id: str
    conversation_id: int


# ---------- Quota ----------

class QuotaOut(BaseModel):
    user_id: int
    daily_limit: int
    monthly_limit: int
    used_today: int
    used_month: int
    day_anchor: date
    month_anchor: str
    percent_today: float
    percent_month: float


class QuotaUpdate(BaseModel):
    daily_limit: Optional[int] = Field(default=None, ge=0)
    monthly_limit: Optional[int] = Field(default=None, ge=0)


# ---------- Audit ----------

class AuditOut(BaseModel):
    id: int
    user_id: int
    conversation_id: Optional[int] = None
    prompt: str
    refs: Optional[Any] = None
    tools_called: Optional[Any] = None
    model: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    status: str
    error_msg: Optional[str] = None
    created_at: datetime


class AuditPage(BaseModel):
    items: List[AuditOut]
    total: int
    next_offset: Optional[int] = None


# ---------- Scheduled query ----------

class ScheduledQueryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=4000)
    refs: List[EntityRef] = Field(default_factory=list, max_length=10)
    cron_expr: str = Field(..., min_length=1, max_length=64)
    timezone: str = Field(default="Asia/Kolkata", max_length=64)


class ScheduledQueryUpdate(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    cron_expr: Optional[str] = None
    timezone: Optional[str] = None
    is_active: Optional[bool] = None


class ScheduledQueryOut(BaseModel):
    id: int
    user_id: int
    name: str
    prompt: str
    refs: Optional[Any] = None
    cron_expr: str
    timezone: str
    is_active: bool
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    created_at: datetime
    pending_approval: bool = False


# ---------- Anomaly subscription ----------

AnomalyMetric = Literal[
    "stuck_candidates",      # candidates in same stage > N days
    "conversion_drop",       # joined-rate vs trailing-window
    "sla_breach",            # job past deadline with N positions open
    "no_activity",           # job with zero stage moves in window
]


class AnomalySubCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    metric_key: AnomalyMetric
    params: dict = Field(..., description="metric-specific thresholds + scope")
    cooldown_min: int = Field(default=360, ge=15, le=10080)


class AnomalySubUpdate(BaseModel):
    name: Optional[str] = None
    params: Optional[dict] = None
    cooldown_min: Optional[int] = Field(default=None, ge=15, le=10080)
    is_active: Optional[bool] = None


class AnomalySubOut(BaseModel):
    id: int
    user_id: int
    name: str
    metric_key: str
    params: Any
    is_active: bool
    cooldown_min: int
    last_fired_at: Optional[datetime] = None
    created_at: datetime
    pending_approval: bool = False


# ---------- Approval queue ----------

ApprovalDecision = Literal["approve", "decline"]


class ApprovalOut(BaseModel):
    id: int
    user_id: int
    origin: str
    payload: Any
    status: str
    approver_role: str
    target_kind: Optional[str] = None
    target_id: Optional[int] = None
    decided_by: Optional[int] = None
    decided_at: Optional[datetime] = None
    created_at: datetime


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision


class ApprovalsPage(BaseModel):
    items: List[ApprovalOut]
    total: int


# ---------- Quota list (SuperAdmin) ----------

class UserQuotaRow(BaseModel):
    user_id: int
    name: Optional[str] = None
    username: Optional[str] = None
    role_name: Optional[str] = None
    daily_limit: int
    monthly_limit: int
    used_today: int
    used_month: int


class UserQuotaList(BaseModel):
    items: List[UserQuotaRow]
    total: int
