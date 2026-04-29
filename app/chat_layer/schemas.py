"""Pydantic schemas for chat REST API."""
from datetime import datetime
from typing import Any, List, Literal, Optional, Union
from pydantic import BaseModel, Field, model_validator

MessageType = Literal["text", "image", "voice", "file", "system"]
EntityType = Literal[
    "job", "candidate", "company", "pipeline", "user", "team", "report",
]


class EntityRef(BaseModel):
    """A `(type, id)` pair embedded in a message. Body text holds an
    @@ref:type:id@@ token at the position the matching card should render.

    `params` carries per-type extra context. For reports it holds the
    selected filters (date_from, date_to, granularity, etc.) so the card
    snapshot and the click-through URL can both reflect them.
    """
    type: EntityType
    # Numeric for everything except `report` (which uses string slugs)
    # and `candidate` (which uses string `candidate_id`).
    id: Union[int, str]
    params: Optional[dict] = None


class EntityField(BaseModel):
    label: str
    value: str


class EntityCard(BaseModel):
    """Resolver output — the renderable preview the FE shows in chat."""
    type: EntityType
    id: Union[int, str]
    title: str
    subtitle: Optional[str] = None
    status: Optional[str] = None
    status_color: Optional[str] = None
    deep_link: str
    avatar_url: Optional[str] = None
    fields: List[EntityField] = []
    # For reports: which echarts shape the FE should render as a snapshot
    # (line | funnel | bar | donut | table). Echoed back verbatim from the
    # catalog so the snapshot component can pick its template.
    chart_type: Optional[str] = None
    # Echo of the params used so the FE can render a filter summary chip.
    params: Optional[dict] = None


class CreateDMRequest(BaseModel):
    peer_user_id: int = Field(..., gt=0)


class SendMessageRequest(BaseModel):
    message_type: MessageType = "text"
    body: Optional[str] = Field(default=None, max_length=4000)
    attachment_id: Optional[int] = Field(default=None, gt=0)
    reply_to_message_id: Optional[int] = Field(default=None, gt=0)
    # Entity references embedded in `body` via @@ref:type:id@@ tokens.
    # Caller may include this even when body has no tokens — useful when
    # the card is the entire content (text body becomes a chip-only).
    refs: List[EntityRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _content_required(self):
        if self.message_type == "text":
            has_text = bool(self.body and self.body.strip())
            has_refs = bool(self.refs)
            if not has_text and not has_refs:
                raise ValueError(
                    "body or at least one ref required for text messages",
                )
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
    is_system: bool = False
    message_type: str
    body: Optional[str] = None
    attachment: Optional[AttachmentOut] = None
    # Resolved cards for any embedded references. The FE renders a card per
    # entry; the ordering matches the @@ref:type:id@@ tokens in `body`.
    refs: List[EntityCard] = []
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
