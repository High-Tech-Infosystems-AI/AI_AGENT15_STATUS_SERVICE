"""Pydantic request/response models for the mail API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------- accounts
class AccountCreate(BaseModel):
    display_name: str
    email_address: EmailStr
    provider: str = "custom"
    smtp_host: str
    smtp_port: int = 587
    smtp_security: str = "starttls"       # ssl | starttls | none
    smtp_username: Optional[str] = None    # defaults to email_address
    smtp_password: str
    imap_host: str
    imap_port: int = 993
    imap_security: str = "ssl"
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    use_same_credentials: bool = True
    sync_enabled: bool = True
    sync_interval_seconds: int = 300
    backfill_days: int = 90
    use_idle: bool = False
    consent_acknowledged: bool = False


class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    provider: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_security: Optional[str] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None    # only re-encrypted if provided
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_security: Optional[str] = None
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    use_same_credentials: Optional[bool] = None
    sync_enabled: Optional[bool] = None
    sync_interval_seconds: Optional[int] = None
    backfill_days: Optional[int] = None
    use_idle: Optional[bool] = None


class AccountOut(BaseModel):
    id: int
    display_name: str
    email_address: str
    provider: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    imap_host: str
    imap_port: int
    imap_security: str
    imap_username: str
    use_same_credentials: bool
    password_set: bool
    sync_enabled: bool
    sync_interval_seconds: int
    backfill_days: int
    use_idle: bool
    status: str
    last_error: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    is_default: bool


class TestConnectionRequest(BaseModel):
    smtp_host: str
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str
    smtp_password: Optional[str] = None
    imap_host: str
    imap_port: int = 993
    imap_security: str = "ssl"
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    use_same_credentials: bool = True
    account_id: Optional[int] = None       # test a saved account (reuse stored pw)


class ConnStep(BaseModel):
    step: str
    ok: bool
    detail: Optional[str] = None


class TestConnectionResult(BaseModel):
    ok: bool
    steps: List[ConnStep]
    folders_found: Optional[int] = None


# ---------------------------------------------------------------- folders
class FolderCreate(BaseModel):
    name: str
    parent_folder_id: Optional[int] = None


class FolderUpdate(BaseModel):
    name: Optional[str] = None
    parent_folder_id: Optional[int] = None


class FolderOut(BaseModel):
    id: int
    name: str
    role: str
    parent_folder_id: Optional[int] = None
    unread_count: int
    total_count: int
    sort_order: int
    is_system: bool


# ---------------------------------------------------------------- messages
class Address(BaseModel):
    name: Optional[str] = ""
    address: str


class MessageListItem(BaseModel):
    id: int
    folder_id: int
    conversation_id: Optional[str] = None
    direction: str
    from_name: Optional[str] = None
    from_address: Optional[str] = None
    to: List[Address] = Field(default_factory=list)
    subject: Optional[str] = None
    snippet: Optional[str] = None
    has_attachments: bool
    is_read: bool
    is_flagged: bool
    is_draft: bool
    importance: str
    send_status: Optional[str] = None
    internal_date: Optional[datetime] = None
    categories: List[int] = Field(default_factory=list)


class AttachmentOut(BaseModel):
    id: int
    file_name: str
    mime_type: str
    size_bytes: int
    is_inline: bool
    content_id: Optional[str] = None
    fetched: bool


class MessageOut(MessageListItem):
    cc: List[Address] = Field(default_factory=list)
    bcc: List[Address] = Field(default_factory=list)
    reply_to_address: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    in_reply_to: Optional[str] = None
    message_id_hdr: Optional[str] = None
    sent_at: Optional[datetime] = None
    attachments: List[AttachmentOut] = Field(default_factory=list)


class MoveRequest(BaseModel):
    folder_id: int


class CategoriesRequest(BaseModel):
    category_ids: List[int]


class BulkRequest(BaseModel):
    ids: List[int]
    action: str                            # read|unread|flag|unflag|move|trash|junk|delete|categorize
    folder_id: Optional[int] = None        # for move
    category_ids: Optional[List[int]] = None


# ---------------------------------------------------------------- compose
class DraftCreate(BaseModel):
    account_id: Optional[int] = None       # which mailbox to send from
    to: List[str] = Field(default_factory=list)
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    subject: Optional[str] = ""
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    importance: str = "normal"
    in_reply_to: Optional[str] = None
    attachment_ids: List[int] = Field(default_factory=list)
    signature_id: Optional[int] = None


class DraftUpdate(DraftCreate):
    pass


class SendRequest(BaseModel):
    draft_id: Optional[int] = None
    account_id: Optional[int] = None       # which mailbox to send from
    # inline send (no pre-saved draft)
    to: List[str] = Field(default_factory=list)
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    subject: Optional[str] = ""
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    importance: str = "normal"
    in_reply_to: Optional[str] = None
    attachment_ids: List[int] = Field(default_factory=list)
    request_read_receipt: Optional[bool] = None


class ScheduleRequest(SendRequest):
    scheduled_at: datetime


# ---------------------------------------------------------------- settings
class SettingsOut(BaseModel):
    focused_inbox: bool
    conversation_view: bool
    reading_pane: str
    preview_lines: int
    mark_read_behavior: str
    mark_read_delay_seconds: int
    external_images: str
    compose_format: str
    default_font: str
    default_font_size: int
    undo_send_seconds: int
    request_read_receipt: bool
    request_delivery_receipt: bool
    read_receipt_response: str
    empty_trash_on_exit: bool
    prefs_json: Optional[Any] = None


class SettingsUpdate(BaseModel):
    focused_inbox: Optional[bool] = None
    conversation_view: Optional[bool] = None
    reading_pane: Optional[str] = None
    preview_lines: Optional[int] = None
    mark_read_behavior: Optional[str] = None
    mark_read_delay_seconds: Optional[int] = None
    external_images: Optional[str] = None
    compose_format: Optional[str] = None
    default_font: Optional[str] = None
    default_font_size: Optional[int] = None
    undo_send_seconds: Optional[int] = None
    request_read_receipt: Optional[bool] = None
    request_delivery_receipt: Optional[bool] = None
    read_receipt_response: Optional[str] = None
    empty_trash_on_exit: Optional[bool] = None
    prefs_json: Optional[Any] = None


class AutoReplyOut(BaseModel):
    auto_reply_enabled: bool
    auto_reply_subject: Optional[str] = None
    auto_reply_message_html: Optional[str] = None
    auto_reply_start: Optional[datetime] = None
    auto_reply_end: Optional[datetime] = None
    auto_reply_internal_only: bool


class AutoReplyUpdate(BaseModel):
    auto_reply_enabled: bool = False
    auto_reply_subject: Optional[str] = None
    auto_reply_message_html: Optional[str] = None
    auto_reply_start: Optional[datetime] = None
    auto_reply_end: Optional[datetime] = None
    auto_reply_internal_only: bool = False


class ForwardingUpdate(BaseModel):
    forwarding_enabled: bool = False
    forwarding_address: Optional[str] = None
    forwarding_keep_copy: bool = True


# ---------------------------------------------------------------- signatures / rules / senders / categories
class SignatureIn(BaseModel):
    name: str
    body_html: str
    is_default_new: bool = False
    is_default_reply: bool = False


class SignatureOut(SignatureIn):
    id: int


class RuleIn(BaseModel):
    name: str
    is_enabled: bool = True
    priority: int = 0
    conditions_json: List[dict]
    actions_json: List[dict]
    stop_processing: bool = False


class RuleOut(RuleIn):
    id: int


class SenderIn(BaseModel):
    list_type: str                         # blocked | safe
    address_or_domain: str


class SenderOut(SenderIn):
    id: int


class CategoryIn(BaseModel):
    name: str
    color: str = "blue"


class CategoryOut(CategoryIn):
    id: int
