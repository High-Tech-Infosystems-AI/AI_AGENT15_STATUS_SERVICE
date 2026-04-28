"""Pydantic schemas for chat REST API."""
from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, model_validator

MessageType = Literal["text", "image", "voice", "file", "system"]


class CreateDMRequest(BaseModel):
    peer_user_id: int = Field(..., gt=0)


class SendMessageRequest(BaseModel):
    message_type: MessageType = "text"
    body: Optional[str] = Field(default=None, max_length=4000)
    attachment_id: Optional[int] = Field(default=None, gt=0)
    reply_to_message_id: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _content_required(self):
        if self.message_type == "text":
            if not self.body or not self.body.strip():
                raise ValueError("body required for text messages")
        else:
            if self.attachment_id is None:
                raise ValueError("attachment_id required for non-text messages")
        return self


class EditMessageRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)


class ForwardMessageRequest(BaseModel):
    conversation_ids: List[int] = Field(..., min_length=1, max_length=20)


class MarkReadRequest(BaseModel):
    message_id: int = Field(..., gt=0)


class MarkReadBulkRequest(BaseModel):
    """Bulk mark-read: pass any number of message_ids in one call.
    Server processes them best-effort: members-only / non-existent ids are
    silently skipped. Replies 204 No Content; the per-message read events
    fire over the WS as if you'd called the single-message endpoint N times.
    """
    message_ids: List[int] = Field(..., min_length=1, max_length=200)


class AddReactionRequest(BaseModel):
    """Body for `POST /chat/messages/{id}/reactions`. The same user can have
    multiple distinct emoji reactions on the same message — but trying to
    add the same emoji twice is idempotent (no error, no duplicate row)."""
    emoji: str = Field(..., min_length=1, max_length=32)


class ReactionUser(BaseModel):
    user_id: int
    username: Optional[str] = None
    name: Optional[str] = None


class ReactionGroup(BaseModel):
    """One emoji on a message + the count and the list of who reacted with
    it. The client renders this as a pill: e.g. `👍 3` with hover showing
    "Alice, Bob, Carol"."""
    emoji: str
    count: int
    users: List[ReactionUser] = []


class LatestMessagePreview(BaseModel):
    id: int
    sender_id: int
    message_type: str
    body_preview: str
    created_at: datetime
    deleted_at: Optional[datetime] = None


class PeerInfo(BaseModel):
    id: int
    name: str
    username: str
    profile_image_key: Optional[str] = None
    profile_image_url: Optional[str] = None


class TeamInfo(BaseModel):
    id: int
    name: str


class ConversationOut(BaseModel):
    id: int
    type: str
    team_id: Optional[int] = None
    title: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread_count: int = 0
    members: List[int] = []
    latest_message: Optional[LatestMessagePreview] = None
    peer: Optional[PeerInfo] = None
    team: Optional[TeamInfo] = None

    model_config = {"from_attributes": True}


class AttachmentOut(BaseModel):
    id: int
    mime_type: str
    file_name: str
    size_bytes: int
    duration_seconds: Optional[int] = None
    waveform_json: Optional[str] = None
    url: Optional[str] = None
    thumbnail_url: Optional[str] = None

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    sender_id: int
    sender_username: Optional[str] = None
    sender_name: Optional[str] = None
    message_type: str
    body: Optional[str] = None
    attachment: Optional[AttachmentOut] = None
    reply_to_message_id: Optional[int] = None
    forwarded_from_message_id: Optional[int] = None
    forwarded_from_sender_id: Optional[int] = None
    forwarded_from_sender_username: Optional[str] = None
    forwarded_from_sender_name: Optional[str] = None
    edited_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    created_at: datetime
    mentions: List[int] = []
    read_count: Optional[int] = None
    delivered_count: Optional[int] = None
    # Persistent state — populated by GET endpoints from chat_message_reads /
    # chat_message_deliveries so clients can render correct ticks after reload
    # without depending on volatile WS state.
    read_by: List[int] = []
    delivered_to: List[int] = []
    # Reactions grouped by emoji. Empty when no reactions yet.
    reactions: List[ReactionGroup] = []

    model_config = {"from_attributes": True}


class PresenceOut(BaseModel):
    user_id: int
    status: str
    last_seen_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PaginatedMessages(BaseModel):
    items: List[MessageOut]
    next_cursor: Optional[str] = None
    has_more: bool


class ErrorResponse(BaseModel):
    error_code: str
    message: str
