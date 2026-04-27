# Chat Service API Documentation

> **Service:** `HRMIS_CHAT_SERVICE` (runs as `app.chat_main:app` on port **8517**)
> **Discovery:** Consul tag `path=/chat` — gateway routes `/chat/*` automatically
> **Companion service:** `HRMIS_STATUS_SERVICE` on port 8515 (notifications, status)
> **Date:** 2026-04-27 · **Version:** 1.0

This document describes every REST endpoint and WebSocket event exposed by the chat service: payloads, responses, behavior, RBAC rules, and end-to-end examples.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Authentication](#2-authentication)
3. [Concepts](#3-concepts)
4. [REST API — Conversations](#4-rest-api--conversations)
5. [REST API — Messages](#5-rest-api--messages)
6. [REST API — Attachments](#6-rest-api--attachments)
7. [REST API — Presence](#7-rest-api--presence)
8. [REST API — Search](#8-rest-api--search)
9. [WebSocket Protocol](#9-websocket-protocol)
10. [End-to-End Flows](#10-end-to-end-flows)
11. [Error Reference](#11-error-reference)
12. [RBAC Reference](#12-rbac-reference)
13. [Limits & Quotas](#13-limits--quotas)

---

## 1. Overview

### 1.1 What it is

The Chat service provides WhatsApp-style real-time messaging for the recruitment platform:

- **1:1 DMs** between any two active users
- **Team chats** for every team in `team_members` (lazy-created on first access)
- **`#general`** — single org-wide room every active user belongs to
- Real-time delivery with **presence**, **typing indicators**, **read receipts**, **last-seen**
- **Attachments** (images, voice notes, files) stored privately in S3
- **Reply**, **forward**, **edit**, **soft-delete** (delete is admin-only)
- **WhatsApp-style formatting** (bold/italic/strike/code) and **`@mentions`**
- Server-side **search** scoped to conversations the caller is a member of
- Offline recipients receive a row in the existing `notifications` table and a push event over `/ws/notifications` (the existing notification socket on the Status service)

### 1.2 Topology

```
┌────────────────────────── Pod / Container ──────────────────────────┐
│                                                                     │
│  app.main:app          app.chat_main:app         notification_ui    │
│  port 8515             port 8517                 port 5009          │
│  /status/*             /chat/* + /chat/ws        (web UI)           │
│  /health               /chat/health                                 │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
        │                       │
        ▼                       ▼
 Consul: STATUS_SERVICE   Consul: CHAT_SERVICE
 tag: path=/status        tag: path=/chat
        │                       │
        └──────── API Gateway ──┘   (routes by Consul tag)
```

Both services share the same MySQL (`ats_main`) and Redis. Chat publishes real-time events to `chat:user:{id}` channels; offline alerts go through the existing `notif:user:{id}` channels (consumed by the Status service's notification WebSocket).

### 1.3 Base URLs

| Environment | Base URL (through gateway) | Direct (bypass gateway) |
|---|---|---|
| Local | `http://localhost:8000/chat` | `http://localhost:8517/chat` |
| Production | `https://<gateway-host>/chat` | internal `http://chat-service:8517/chat` |

For WebSocket: `ws://<host>/chat/ws?token=<jwt>` (gateway must allow WS upgrade).

### 1.4 Conventions

- All requests/responses use `Content-Type: application/json` unless documented otherwise (file upload uses `multipart/form-data`).
- Timestamps are ISO-8601 in UTC (e.g. `"2026-04-27T12:00:00"`). The server returns naïve ISO strings — clients should treat them as UTC.
- IDs are integers. `message_id` is `BIGINT` (large), all others are `INT`.
- Pagination uses **opaque cursors** (base64-encoded). Don't parse them; pass them back verbatim.

---

## 2. Authentication

### 2.1 REST endpoints

Every REST endpoint requires a JWT in the `Authorization` header:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiI...
```

The chat service validates this token by calling the auth service (`AUTH_SERVICE_URL`). Successful validations are cached in Redis for 60 seconds (key `auth:token:<sha256>`).

If validation fails, the response is:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"detail": "Invalid or expired token"}
```

If the header is missing or malformed:

```http
HTTP/1.1 401 Unauthorized

{"detail": "Missing bearer token"}
```

### 2.2 WebSocket

The WebSocket uses a query-string token because browsers can't easily set headers on a `WebSocket()` constructor:

```
ws://host:8517/chat/ws?token=<jwt>
```

Closure code `4001` is sent if the token is invalid:

```javascript
ws.onclose = (e) => {
  if (e.code === 4001) console.error("Auth failed:", e.reason);
};
```

### 2.3 Active-user requirement

A user is considered **active** when:
- `users.deleted_at IS NULL`
- `users.enable = 1`

Inactive users cannot be messaged (DMs are blocked) and cannot have a conversation list returned.

---

## 3. Concepts

### 3.1 Conversation types

| `type` | Meaning | Membership |
|---|---|---|
| `dm` | One-to-one DM | Exactly two users |
| `team` | Team chat (one per team) | All `team_members` for that team |
| `general` | Org-wide `#general` | Every active user (lazy-joined on first access) |

There is exactly **one** `general` conversation in the system (`id=1`, seeded by `v12_chat_general_seed.sql`).

### 3.2 Message types

| `message_type` | Has `body` | Requires `attachment_id` | Notes |
|---|---|---|---|
| `text` | yes (1–4000 chars) | no | Subject to WhatsApp formatting + mentions |
| `image` | optional caption | yes | Pre-signed thumbnail + full URL returned |
| `voice` | no | yes | `duration_seconds` + waveform JSON |
| `file` | optional caption | yes | Generic file (PDF, doc, zip, etc.) |
| `system` | yes | no | System-generated (e.g. "Alice joined") — not user-creatable |

### 3.3 RBAC summary (full table in §12)

- **DM:** any active user ↔ any active user
- **Team chat:** SuperAdmin/Admin → any team; others → only their member teams
- **`#general`:** all active users
- **Edit:** only sender, only `text`, only within 15 min
- **Delete:** SuperAdmin/Admin only (soft-delete)
- **Forward:** must be member of every destination

### 3.4 Lifecycle of a sent message

```
Client (REST)                 Chat Server                 Other Clients (WS)
     │                              │                              │
     │ POST /chat/conv/5/messages   │                              │
     ├─────────────────────────────►│                              │
     │                              │ 1. ACL check                 │
     │                              │ 2. Sanitize body             │
     │                              │ 3. Persist chat_messages     │
     │                              │ 4. Resolve @mentions         │
     │                              │ 5. For each member:          │
     │                              │    - online → publish to     │
     │                              │      chat:user:{id}          │
     │                              │    - offline → insert        │
     │                              │      notification + publish  │
     │                              │      to notif:user:{id}      │
     │                              │                              │
     │  200 OK (MessageOut)         │       message.new event ────►│
     │◄─────────────────────────────┤                              │
```

---

## 4. REST API — Conversations

All conversation endpoints are mounted at `/chat/conversations`.

### 4.1 List my conversations (WhatsApp-style inbox)

```http
GET /chat/conversations
Authorization: Bearer <jwt>
```

Returns the **inbox** for the caller — every conversation they belong to, enriched with peer/team display info, the latest message preview, and an unread badge count. Auto-joins the caller into `#general` if they're not yet a member.

**Sorted by** `last_message_at DESC NULLS LAST`, then `id DESC` — so the newest activity is on top, just like WhatsApp.

**Response:** `200 OK`

```json
[
  {
    "id": 42,
    "type": "dm",
    "team_id": null,
    "title": null,
    "last_message_at": "2026-04-27T12:01:14",
    "unread_count": 2,
    "members": [10, 11],
    "peer": {
      "id": 11,
      "name": "Bob Patel",
      "username": "bob",
      "profile_image_key": "profiles/user_11/abc.jpg"
    },
    "team": null,
    "latest_message": {
      "id": 992,
      "sender_id": 11,
      "message_type": "text",
      "body_preview": "see you tomorrow!",
      "created_at": "2026-04-27T12:01:14",
      "deleted_at": null
    }
  },
  {
    "id": 17,
    "type": "team",
    "team_id": 4,
    "title": null,
    "last_message_at": "2026-04-27T11:50:01",
    "unread_count": 5,
    "members": [10, 11, 12],
    "peer": null,
    "team": { "id": 4, "name": "Backend Team" },
    "latest_message": {
      "id": 871,
      "sender_id": 12,
      "message_type": "voice",
      "body_preview": "[voice note]",
      "created_at": "2026-04-27T11:50:01",
      "deleted_at": null
    }
  },
  {
    "id": 1,
    "type": "general",
    "team_id": null,
    "title": "#general",
    "last_message_at": "2026-04-27T11:45:33",
    "unread_count": 0,
    "members": [1, 7, 10, 22, 38],
    "peer": null,
    "team": null,
    "latest_message": {
      "id": 803,
      "sender_id": 7,
      "message_type": "text",
      "body_preview": "office shut early today",
      "created_at": "2026-04-27T11:45:33",
      "deleted_at": null
    }
  }
]
```

#### Field reference

| Field | Notes |
|---|---|
| `peer` | Populated **only** for `type=dm`. Contains the **other** user's `id`, `name`, `username`, `profile_image_key`. Use the `profile_image_key` against the RBAC service's profile-image URL endpoint to render the avatar. |
| `team` | Populated only for `type=team`. The `name` is what should be shown in the inbox cell ("Backend Team"). |
| `latest_message.body_preview` | First 140 chars of the last message body. For non-text messages it's a localised placeholder: `[image]`, `[voice note]`, `[file]`. Soft-deleted: `[message deleted]`. |
| `unread_count` | Number of messages newer than the caller's `last_read_message_id` (i.e. not yet acknowledged via `mark_read`). Excludes messages the caller sent themselves and soft-deleted ones. |
| `members` | All member user IDs (caller included). Use this to identify "self" when rendering. |

#### How the WhatsApp-style badge updates in real time

The chat WebSocket (`/chat/ws`) already drives this — **no separate inbox WebSocket needed.** Three events keep the inbox in sync:

1. **`message.new`** (existing) — fires for every new message in any conversation the user belongs to. The client increments `unread_count` for that conversation locally.
2. **`inbox.bump`** (new) — fired alongside `message.new` to every member (and the sender). Carries the same `latest_message` preview and an authoritative `unread_count` so clients can re-render the inbox cell without recomputing.
3. **`unread.update`** (new) — self-loopback for cross-tab sync. When the user calls `mark_read` on tab A, all of *their* other tabs receive `unread.update` so the badge clears everywhere.

See §9.2 for payload schemas.

---

### 4.2 Create or get a DM

```http
POST /chat/conversations/dm
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "peer_user_id": 11
}
```

Idempotent: returns the existing DM if one already exists between these two users; otherwise creates one. The two members are stored in canonical (sorted) order; calling with `(10, 11)` and `(11, 10)` yields the same conversation.

**Validation:**
- `peer_user_id` must be a positive integer.
- The peer must be an **active user** (`enable=1`, not soft-deleted).
- The peer must be different from the caller.

**Response:** `200 OK`

```json
{
  "id": 42,
  "type": "dm",
  "team_id": null,
  "title": null,
  "last_message_at": null,
  "unread_count": 0,
  "members": [10, 11]
}
```

**Errors:**

| Status | `error_code` | When |
|---|---|---|
| 403 | `CHAT_USER_INACTIVE` | Peer user is disabled or soft-deleted |
| 422 | (validation error) | `peer_user_id` missing/invalid |

---

### 4.3 Get a team conversation (lazy-create)

```http
GET /chat/conversations/team/{team_id}
Authorization: Bearer <jwt>
```

Returns the team's chat. Lazy-creates on first access, populating members from `team_members`. Subsequent calls reconcile membership (adds users joined since the last access).

**RBAC:** SuperAdmin/Admin can access any team. Others must be in `team_members` for that team.

**Response:** `200 OK`

```json
{
  "id": 17,
  "type": "team",
  "team_id": 4,
  "title": null,
  "last_message_at": null,
  "unread_count": 0,
  "members": [10, 11, 12, 14]
}
```

**Errors:**

| Status | `error_code` | When |
|---|---|---|
| 403 | `CHAT_TEAM_MEMBERSHIP_REQUIRED` | Caller is not Admin/SuperAdmin and not a team member |

---

### 4.4 Get #general

```http
GET /chat/conversations/general
Authorization: Bearer <jwt>
```

Returns the singleton `#general` conversation, auto-joining the caller as a member if they aren't already.

**Response:** `200 OK`

```json
{
  "id": 1,
  "type": "general",
  "team_id": null,
  "title": "#general",
  "last_message_at": "2026-04-27T11:45:33",
  "unread_count": 0,
  "members": [1, 7, 10, 11, 22, 38]
}
```

---

### 4.5 Get a conversation by id

```http
GET /chat/conversations/{conversation_id}
Authorization: Bearer <jwt>
```

**RBAC:** caller must be a member of the conversation.

**Response:** same shape as 4.1 entries.

**Errors:**

| Status | `error_code` |
|---|---|
| 403 | `CHAT_NOT_MEMBER` |
| 404 | `CHAT_NOT_FOUND` (deleted or doesn't exist) |

---

## 5. REST API — Messages

### 5.1 List messages in a conversation (paginated)

```http
GET /chat/conversations/{conversation_id}/messages?cursor={cursor}&limit={n}
Authorization: Bearer <jwt>
```

Returns messages in **reverse chronological order** (newest first). Use the returned `next_cursor` to fetch the next page (older messages).

**Query params:**
- `cursor` — opaque base64 cursor from a previous response (omit for first page)
- `limit` — 1–100, default 50

**Response:** `200 OK`

```json
{
  "items": [
    {
      "id": 991,
      "conversation_id": 42,
      "sender_id": 10,
      "message_type": "text",
      "body": "see you tomorrow!",
      "attachment": null,
      "reply_to_message_id": null,
      "forwarded_from_message_id": null,
      "edited_at": null,
      "deleted_at": null,
      "created_at": "2026-04-27T12:01:14",
      "mentions": [],
      "read_count": null,
      "delivered_count": null
    },
    {
      "id": 990,
      "conversation_id": 42,
      "sender_id": 11,
      "message_type": "voice",
      "body": null,
      "attachment": {
        "id": 88,
        "mime_type": "audio/webm",
        "file_name": "voice.webm",
        "size_bytes": 240000,
        "duration_seconds": 12,
        "waveform_json": "[0.1,0.4,0.7,...]",
        "url": "https://s3.../chat/42/2026-04/abc.webm?X-Amz-Signature=...",
        "thumbnail_url": null
      },
      "reply_to_message_id": 989,
      "forwarded_from_message_id": null,
      "edited_at": null,
      "deleted_at": null,
      "created_at": "2026-04-27T12:00:30",
      "mentions": [],
      "read_count": null,
      "delivered_count": null
    }
  ],
  "next_cursor": "eyJ0IjogIjIwMjYtMDQtMjdUMTI6MDA6MzAiLCAiaSI6IDk5MH0=",
  "has_more": true
}
```

**Notes:**
- A soft-deleted message has `deleted_at` set, `body` returned as `"[message deleted]"`, and `attachment` is `null`.
- `read_count` and `delivered_count` are reserved for team-room read counts; currently `null` in list responses (push happens via WebSocket events).

**Errors:** `403 CHAT_NOT_MEMBER` if the caller is not in the conversation.

---

### 5.2 Send a message

```http
POST /chat/conversations/{conversation_id}/messages
Authorization: Bearer <jwt>
Content-Type: application/json
```

#### Body

| Field | Type | Required | Description |
|---|---|---|---|
| `message_type` | `"text" \| "image" \| "voice" \| "file"` | yes (default `"text"`) | Determines which fields are required |
| `body` | string (1–4000) | required for `text` | Optional caption for image/file |
| `attachment_id` | integer | required for `image`/`voice`/`file` | From a prior `POST /chat/attachments` |
| `reply_to_message_id` | integer | no | Quote a previous message |

**Examples:**

Plain text:
```json
{ "message_type": "text", "body": "*bold* hi @alice 🎉" }
```

Image with caption:
```json
{
  "message_type": "image",
  "body": "screenshot of the bug",
  "attachment_id": 88
}
```

Voice note:
```json
{ "message_type": "voice", "attachment_id": 91 }
```

Reply to message id 990:
```json
{
  "message_type": "text",
  "body": "agreed",
  "reply_to_message_id": 990
}
```

#### Server behaviour

1. **ACL** check (`_authorize_post`):
   - DM → peer must be active.
   - Team → SuperAdmin/Admin or team member.
   - `#general` → any active user (caller is auto-joined if needed).
2. **Sanitize body** — strips HTML/script tags, escapes `<` `>`. WhatsApp markers (`*` `_` `~` `` ` ``) are preserved verbatim.
3. **Persist** `chat_messages` row.
4. **Resolve @mentions** — regex `@[a-zA-Z][a-zA-Z0-9_]{1,49}`, dedup, lookup active users by `LOWER(username)`. Hits write rows in `chat_message_mentions` and trigger a `chat.mention` notification (priority `high`) — see §10.5.
5. **Update** `chat_conversations.last_message_at`.
6. **Fan out**:
   - For each member except sender:
     - **Online?** → publish to `chat:user:{id}` (WS event `message.new`).
     - **Offline?** → insert a `notifications` row (`domain_type='chat'`, `event_type='chat.message_received'`) + `notification_recipients` + publish to `notif:user:{id}` so the existing notification WS pushes a banner.

#### Response — `200 OK`

```json
{
  "id": 992,
  "conversation_id": 42,
  "sender_id": 10,
  "message_type": "text",
  "body": "*bold* hi @alice 🎉",
  "attachment": null,
  "reply_to_message_id": null,
  "forwarded_from_message_id": null,
  "edited_at": null,
  "deleted_at": null,
  "created_at": "2026-04-27T12:02:01",
  "mentions": [11],
  "read_count": null,
  "delivered_count": null
}
```

#### Errors

| Status | `error_code` | When |
|---|---|---|
| 403 | `CHAT_USER_INACTIVE` | DM peer disabled |
| 403 | `CHAT_TEAM_MEMBERSHIP_REQUIRED` | Team chat without membership |
| 404 | `CHAT_NOT_FOUND` | Conversation deleted/missing |
| 422 | (validation) | Body too long / `text` without `body` / non-text without `attachment_id` |

---

### 5.3 Mark a message as read

```http
POST /chat/messages/{message_id}/read
Authorization: Bearer <jwt>
```

Idempotent. Records that the caller has read this message.

- **DM:** publishes `message.read` to the **sender** (drives WhatsApp blue-ticks).
- **Team / general:** recomputes the read count for the message and publishes `message.read_count` to all members.

**Response:** `204 No Content`

**Errors:**

| Status | `error_code` |
|---|---|
| 403 | `CHAT_NOT_MEMBER` |
| 404 | `CHAT_NOT_FOUND` |

> **Tip:** For chatty UIs (scroll-as-you-read), prefer the WebSocket `mark_read` action over hammering this endpoint.

---

### 5.4 Edit a message

```http
PATCH /chat/messages/{message_id}
Authorization: Bearer <jwt>
Content-Type: application/json

{ "body": "updated text" }
```

**Rules:**
- Only the original **sender** can edit.
- Only `text` messages.
- Within **15 minutes** of `created_at`.
- The previous body is preserved in `chat_message_edits` (audit).

#### Server actions
1. ACL check.
2. Sanitize new body.
3. Update `chat_messages.body` + `edited_at = NOW()`.
4. Publish `message.edited` to every conversation member.

#### Response — `200 OK`

```json
{
  "id": 992,
  "conversation_id": 42,
  "sender_id": 10,
  "message_type": "text",
  "body": "updated text",
  "attachment": null,
  "reply_to_message_id": null,
  "forwarded_from_message_id": null,
  "edited_at": "2026-04-27T12:05:30",
  "deleted_at": null,
  "created_at": "2026-04-27T12:02:01",
  "mentions": [],
  "read_count": null,
  "delivered_count": null
}
```

#### Errors

| Status | `error_code` |
|---|---|
| 403 | `CHAT_EDIT_NOT_OWNER` (caller isn't sender, or message_type ≠ text) |
| 409 | `CHAT_EDIT_WINDOW_EXPIRED` (>15 min since send) |
| 410 | `CHAT_MESSAGE_DELETED` |
| 404 | `CHAT_NOT_FOUND` |

---

### 5.5 Delete a message *(Admin only)*

```http
DELETE /chat/messages/{message_id}
Authorization: Bearer <jwt>
```

**Only SuperAdmin or Admin can delete.** Soft-delete: sets `deleted_at` and `deleted_by`. The body remains in DB for audit; subsequent fetches return `body = "[message deleted]"` and hide the attachment.

#### Server actions
- Mark `chat_messages.deleted_at`, `deleted_by`.
- Publish `message.deleted` to every conversation member.

#### Response — `204 No Content`

#### Errors

| Status | `error_code` |
|---|---|
| 403 | `CHAT_ADMIN_ONLY` (caller is not Admin/SuperAdmin) |
| 404 | `CHAT_NOT_FOUND` |

---

### 5.6 Forward a message

```http
POST /chat/messages/{message_id}/forward
Authorization: Bearer <jwt>
Content-Type: application/json

{ "conversation_ids": [17, 1, 65] }
```

Forwards an existing message into one or more conversations. Each new message:
- has the **caller** as `sender_id` (i.e. you're forwarding it),
- preserves the **original** `forwarded_from_message_id` so clients can show "Forwarded from …",
- copies `body` and `attachment_id` (the same S3 object is referenced — no duplication),
- triggers normal fan-out (`message.new` to every recipient).

**Rules:**
- Caller must be a **member** of every destination. The Admin team-override does NOT relax this — admins still need to be in the destination conversation.
- Cannot forward a soft-deleted message.
- 1–20 destinations per call.

#### Response — `200 OK`

```json
[
  { "id": 993, "conversation_id": 17, "...": "..." },
  { "id": 994, "conversation_id": 1, "...": "..." },
  { "id": 995, "conversation_id": 65, "...": "..." }
]
```

(Each item has the same shape as a `MessageOut` from §5.2.)

#### Errors

| Status | `error_code` | When |
|---|---|---|
| 403 | `CHAT_FORWARD_NOT_MEMBER` | Caller not in one or more destination conversations (response stops at the first failing destination) |
| 410 | `CHAT_MESSAGE_DELETED` |
| 404 | `CHAT_NOT_FOUND` |

---

## 6. REST API — Attachments

### 6.1 Upload an attachment

```http
POST /chat/attachments
Authorization: Bearer <jwt>
Content-Type: multipart/form-data

conversation_id=42
duration_seconds=12        (optional, only for voice)
file=@voice.webm           (the file)
```

Two-step send: **upload first, then post the message** referencing the returned `attachment_id`.

**Server actions:**
1. Detect category from `file.content_type`:
   - **image:** `image/jpeg`, `image/jpg`, `image/png`, `image/webp`, `image/gif` (max 10 MB)
   - **voice:** `audio/webm`, `audio/ogg`, `audio/mp4`, `audio/mpeg` (max 10 MB)
   - **file:** PDF, Word/Excel/PowerPoint (`.doc/.docx/.xls/.xlsx/.ppt/.pptx`), `application/zip`, `text/plain`, plus all image MIMEs (max 50 MB)
2. Reject if MIME is outside the allow-list or size exceeds the category limit.
3. Upload to S3: `chat/{conversation_id}/{yyyy-mm}/{uuid}.{ext}` in bucket `AWS_S3_BUCKET_CHAT`.
4. Persist a `chat_message_attachments` row.
5. Return the row + a freshly minted **pre-signed GET URL** (TTL configured by `AWS_S3_PRESIGNED_TTL_SECONDS`, default 3600 s; URL is memoised in-process for half its lifetime).

#### Response — `200 OK`

```json
{
  "id": 88,
  "mime_type": "audio/webm",
  "file_name": "voice.webm",
  "size_bytes": 240000,
  "duration_seconds": 12,
  "waveform_json": null,
  "url": "https://chat-bucket.s3.amazonaws.com/chat/42/2026-04/abc123.webm?X-Amz-Signature=...",
  "thumbnail_url": null
}
```

> Voice **waveform** is computed client-side (JS `AudioContext` + downsampled peaks array) and stored later via the `attachment_id`. Future enhancement: a `PATCH /chat/attachments/{id}` to set `waveform_json` post-upload — currently it lives as JSON on the message attachment row.

#### Errors

| Status | `error_code` | When |
|---|---|---|
| 413 | `CHAT_ATTACHMENT_TOO_LARGE` | Size exceeds category cap |
| 415 | `CHAT_ATTACHMENT_TYPE_NOT_ALLOWED` | MIME not in allow-list |

---

### 6.2 Get a fresh pre-signed URL for an attachment

```http
GET /chat/attachments/{attachment_id}/url
Authorization: Bearer <jwt>
```

Use this after a long browsing session — the URL embedded in the original message response can expire. This endpoint returns a new pre-signed URL (memoised; same URL is reused across calls within `AWS_S3_PRESIGNED_TTL_SECONDS / 2`).

#### Response — `200 OK`

Same shape as the upload response (with a fresh `url`).

---

## 7. REST API — Presence

### 7.1 Get a user's presence

```http
GET /chat/users/{user_id}/presence
Authorization: Bearer <jwt>
```

Returns the user's online/offline status and last seen time. Always visible to anyone who has a JWT (no per-user privacy controls in v1).

#### Response — `200 OK`

```json
{
  "user_id": 11,
  "status": "online",
  "last_seen_at": "2026-04-27T12:08:42"
}
```

If we have no presence record yet, the user is treated as offline:

```json
{ "user_id": 11, "status": "offline", "last_seen_at": null }
```

> **How presence works.** Every WS connection writes `chat:presence:{user_id}=online` in Redis with TTL **90 s** and is refreshed by client `ping` every 30 s. The DB row in `chat_user_presence` is updated on connect, disconnect, and TTL expiry. Real-time changes are also broadcast as `presence.update` events to every user who shares a conversation with the affected user.

---

## 8. REST API — Search

### 8.1 Search messages

```http
GET /chat/search?q={query}&conversation_id={id}&limit={n}
Authorization: Bearer <jwt>
```

Searches message bodies in conversations the **caller is a member of**. Soft-deleted messages are excluded.

**Query params:**
- `q` — required, the search string. Treated as a FULLTEXT BOOLEAN MODE expression where supported, otherwise a simple `LIKE %q%`.
- `conversation_id` — optional, narrows to one conversation.
- `limit` — 1–100, default 50.

**Response:** `200 OK`

```json
{
  "items": [
    { "id": 992, "conversation_id": 42, "sender_id": 10,
      "message_type": "text", "body": "*bold* hi @alice", "...": "..." },
    { "id": 871, "conversation_id": 17, "sender_id": 12,
      "message_type": "text", "body": "alice did the demo", "...": "..." }
  ],
  "has_more": false,
  "next_cursor": null
}
```

> **Engine fallback.** The store auto-detects whether the FULLTEXT `ft_body` index exists (created by migration `v11`). If not, it falls back to `LIKE`. No client-visible difference.

> **No cross-user leakage.** The query joins on `chat_conversation_members` filtered by the caller's user id, so a user cannot retrieve messages from rooms they don't belong to even if they pass an unrelated `conversation_id`.

---

## 9. WebSocket Protocol

### 9.1 Connect

```
ws://host:8517/chat/ws?token=<jwt>
```

Closure codes:

| Code | Reason |
|---|---|
| `4001` | Invalid or expired token (issued before `accept`) |
| `1000` | Normal close (caller disconnected cleanly) |
| Other | Unexpected disconnect — client should reconnect with backoff |

On a successful accept the server:
1. Sets `chat:presence:{user_id}=online` in Redis (TTL 90 s).
2. Upserts `chat_user_presence.status=online`, `last_seen_at=NOW()`.
3. Fans out a `presence.update` event to every user that shares a conversation with the caller.

Multiple concurrent connections per user are allowed — the manager keeps a `Set[WebSocket]` per user_id, so opening the chat in a second tab still works.

---

### 9.2 Server → Client events

All server-to-client messages are JSON of the form:

```json
{ "type": "<event-type>", "data": { ... } }
```

#### `message.new`
A new message was sent into a conversation the user belongs to.

```json
{
  "type": "message.new",
  "data": {
    "conversation_id": 42,
    "message": {
      "id": 992,
      "conversation_id": 42,
      "sender_id": 10,
      "message_type": "text",
      "body": "*bold* hi @alice",
      "attachment": null,
      "reply_to_message_id": null,
      "forwarded_from_message_id": null,
      "edited_at": null,
      "deleted_at": null,
      "created_at": "2026-04-27T12:02:01",
      "mentions": [11]
    }
  }
}
```

#### `message.edited`

```json
{
  "type": "message.edited",
  "data": {
    "message_id": 992,
    "conversation_id": 42,
    "body": "updated text",
    "edited_at": "2026-04-27T12:05:30"
  }
}
```

#### `message.deleted`

```json
{
  "type": "message.deleted",
  "data": {
    "message_id": 992,
    "conversation_id": 42,
    "deleted_by": 1
  }
}
```

#### `message.delivered` *(DM only)*

Reserved for future use (the server doesn't currently emit this in v1; client should be tolerant of receiving it).

```json
{
  "type": "message.delivered",
  "data": {
    "message_id": 992,
    "user_id": 11,
    "delivered_at": "2026-04-27T12:02:02"
  }
}
```

#### `message.read` *(DM only — sent to the original sender)*

```json
{
  "type": "message.read",
  "data": {
    "message_id": 992,
    "user_id": 11,
    "read_at": "2026-04-27T12:03:15"
  }
}
```

#### `message.read_count` *(team / general — sent to all members)*

```json
{
  "type": "message.read_count",
  "data": {
    "message_id": 871,
    "conversation_id": 17,
    "read_count": 4
  }
}
```

#### `typing.start` / `typing.stop`

```json
{
  "type": "typing.start",
  "data": {
    "conversation_id": 42,
    "user_id": 11
  }
}
```

```json
{
  "type": "typing.stop",
  "data": {
    "conversation_id": 42,
    "user_id": 11
  }
}
```

> **Recommendation:** clients should auto-clear typing indicators after 6 s of silence even without a `typing.stop` (matches the 5-s Redis TTL plus 1 s slack), in case the sender goes offline mid-keystroke.

#### `presence.update`

```json
{
  "type": "presence.update",
  "data": {
    "user_id": 11,
    "status": "online",
    "last_seen_at": "2026-04-27T12:08:42"
  }
}
```

Sent on connect, on disconnect, and on heartbeat-TTL expiry.

#### `inbox.bump`

Sent to every conversation member (sender included) right after a new message lands. Mirrors the same `latest_message` shape returned by `GET /chat/conversations` so the client can swap the inbox cell in place without refetching.

```json
{
  "type": "inbox.bump",
  "data": {
    "conversation_id": 42,
    "unread_count": 2,
    "latest_message": {
      "id": 992,
      "sender_id": 11,
      "message_type": "text",
      "body_preview": "see you tomorrow!",
      "created_at": "2026-04-27T12:01:14",
      "deleted_at": null
    }
  }
}
```

For the **sender's** own copy, `unread_count` is always `0` (they just sent it). For everyone else, the count is recomputed from `chat_conversation_members.last_read_message_id` so it's authoritative even after concurrent reads.

#### `unread.update`

Self-loopback. When the user reads on one tab, this event fires on **every** WS connection of the same user (including the one that triggered the read) so the inbox badge clears everywhere.

```json
{
  "type": "unread.update",
  "data": {
    "conversation_id": 42,
    "unread_count": 0
  }
}
```

Triggered by:
- `POST /chat/messages/{id}/read` (REST)
- `{"action": "mark_read", "message_id": 992}` (WS)

#### `mention`

Reserved. Currently mention notifications go through the existing notification WebSocket (see §10.5). v1 does **not** emit a separate `mention` event on the chat WS — clients should listen on `/ws/notifications` for `chat.mention` events instead.

#### `pong`

Reply to a client `ping`.

```json
{ "type": "pong" }
```

---

### 9.3 Client → Server actions

All client-to-server messages are JSON, sent as text frames:

```json
{ "action": "<action-name>", "...": "..." }
```

#### `ping`

```json
{ "action": "ping" }
```

Refreshes the user's presence TTL in Redis. Server replies with `{"type": "pong"}`. **Send every 30 s** to stay marked online.

#### `typing`

```json
{
  "action": "typing",
  "conversation_id": 42,
  "state": "start"
}
```

`state` is `"start"` or `"stop"`. Server fans out a `typing.start` / `typing.stop` to other conversation members. The Redis typing key has a TTL of 5 s, so silence = automatic stop.

#### `mark_read`

```json
{
  "action": "mark_read",
  "message_id": 991
}
```

Records that the user has read this message. Equivalent to the REST endpoint in §5.3 but cheaper for chatty UIs. Server fans out `message.read` (DM) or `message.read_count` (team / general).

---

### 9.4 Heartbeat & reconnection

A robust client should:

```javascript
const ws = new WebSocket(`ws://host:8517/chat/ws?token=${jwt}`);
let pingTimer;

ws.onopen = () => {
  pingTimer = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: "ping" }));
    }
  }, 30_000);  // every 30 s — server TTL is 90 s
};

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  switch (msg.type) {
    case "message.new":     /* render message */; break;
    case "message.edited":  /* update message body */; break;
    case "message.deleted": /* mark as [deleted] */; break;
    case "message.read":    /* update tick to read */; break;
    case "message.read_count": /* update group read counter */; break;
    case "typing.start":    /* show "X is typing" */; break;
    case "typing.stop":     /* hide indicator */; break;
    case "presence.update": /* update online dot / last seen */; break;
    case "pong":            /* heartbeat ack — ignore */; break;
  }
};

ws.onclose = (e) => {
  clearInterval(pingTimer);
  if (e.code !== 1000) {
    // reconnect with exponential backoff
    setTimeout(connect, Math.min(30_000, 1000 * 2 ** retryCount++));
  }
};
```

---

## 10. End-to-End Flows

### 10.0 WhatsApp-style inbox client recipe

```javascript
// 1. Initial fetch — populates the inbox list
const inbox = await fetch("/chat/conversations", {
  headers: { Authorization: `Bearer ${jwt}` }
}).then(r => r.json());
renderInbox(inbox);   // each cell uses peer.name (DM) / team.name (team) /
                     //  latest_message.body_preview / unread_count badge

// 2. Open WS once for the whole app
const ws = new WebSocket(`/chat/ws?token=${jwt}`);

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  switch (msg.type) {

    // Inbox cell update — preview + badge change atomically
    case "inbox.bump": {
      const cell = inbox.find(c => c.id === msg.data.conversation_id);
      if (cell) {
        cell.latest_message = msg.data.latest_message;
        cell.unread_count   = msg.data.unread_count;
        cell.last_message_at = msg.data.latest_message.created_at;
      }
      // re-sort: newest activity to top
      inbox.sort((a, b) => (b.last_message_at || "").localeCompare(a.last_message_at || ""));
      renderInbox(inbox);
      break;
    }

    // Cross-tab badge clear — when this user reads on another device
    case "unread.update": {
      const cell = inbox.find(c => c.id === msg.data.conversation_id);
      if (cell) {
        cell.unread_count = msg.data.unread_count;
        renderInbox(inbox);
      }
      break;
    }

    // Inside an open conversation thread, you also handle these:
    case "message.new":     /* append to thread, then mark_read if visible */ break;
    case "message.edited":  /* update thread row */; break;
    case "message.deleted": /* swap body to "[message deleted]" */; break;
    case "message.read":    /* DM blue tick on the sender side */; break;
    case "presence.update": /* online dot / last seen */; break;
    case "typing.start":
    case "typing.stop":     /* indicator below cell */; break;
  }
};

// 3. When the user opens a conversation and scrolls past unread messages,
//    fire mark_read over the WS — this triggers unread.update for cross-tab sync
function onMessageVisible(msgId) {
  ws.send(JSON.stringify({ action: "mark_read", message_id: msgId }));
}
```

The two new events (`inbox.bump`, `unread.update`) are layered on top of the existing chat WS — clients only need one socket connection.

---

### 10.1 Two users chatting

```
Alice                                Server                                Bob
  │                                    │                                    │
  │ WS /chat/ws?token=<jwt>            │           WS /chat/ws?token=<jwt>  │
  ├───────────────────────────────────►│◄───────────────────────────────────┤
  │                                    │ presence.update (alice online) ──►│
  │◄── presence.update (bob online)    │                                    │
  │                                    │                                    │
  │ POST /chat/conversations/dm        │                                    │
  │ { peer_user_id: <bob.id> }         │                                    │
  ├───────────────────────────────────►│                                    │
  │◄── 200 { id: 42, type: "dm", ... } │                                    │
  │                                    │                                    │
  │ POST /chat/conversations/42/messages                                    │
  │ { message_type: "text", body: "hi" }│                                    │
  ├───────────────────────────────────►│                                    │
  │◄── 200 MessageOut(id=992)          │ message.new (msg 992) ───────────►│
  │                                    │                                    │
  │                                    │ ◄── { action: "mark_read",        │
  │                                    │       message_id: 992 }            │
  │ ◄── message.read (msg 992 by bob)  │                                    │
```

### 10.2 Sending a voice note

```
Browser:
  1. const blob = await new MediaRecorder(stream, { mimeType: "audio/webm" }).stop();
  2. POST /chat/attachments  (multipart, file=blob, conversation_id=42, duration_seconds=12)
     → 200 { id: 88, url: "https://s3.../voice.webm?...", duration_seconds: 12, ... }
  3. POST /chat/conversations/42/messages
     { message_type: "voice", attachment_id: 88 }
     → 200 MessageOut(id=993)

Recipient receives via WS:
  { type: "message.new",
    data: { conversation_id: 42,
            message: { id: 993, message_type: "voice",
                       attachment: { url: "...", duration_seconds: 12, waveform_json: "..." } } } }
```

### 10.3 Reply + forward

Reply:
```http
POST /chat/conversations/42/messages
{ "message_type": "text", "body": "agreed", "reply_to_message_id": 991 }
```

Forward to two rooms:
```http
POST /chat/messages/991/forward
{ "conversation_ids": [17, 1] }
```

Each destination receives a `message.new` event with `forwarded_from_message_id: 991`.

### 10.4 Editing & deleting

Edit own message (within 15 min):
```http
PATCH /chat/messages/991
{ "body": "actually, see you Friday" }
```

Other members receive:
```json
{ "type": "message.edited", "data": { "message_id": 991, "body": "actually, see you Friday", ... } }
```

Admin deletes:
```http
DELETE /chat/messages/991
```

All members receive:
```json
{ "type": "message.deleted", "data": { "message_id": 991, "conversation_id": 42, "deleted_by": 1 } }
```

### 10.5 Mentions and offline notifications

When Alice posts `"hey @bob can you check this"`:

1. Server resolves `@bob` → `bob.id = 11`.
2. Inserts row in `chat_message_mentions(message_id, mentioned_user_id=11)`.
3. **Always** (even if Bob is online) — inserts a `notifications` row:
   ```
   domain_type    = 'chat'
   delivery_mode  = 'push'
   priority       = 'high'
   event_type     = 'chat.mention'
   target_type    = 'user'
   target_id      = '11'
   title          = 'Alice mentioned you'
   message        = 'hey @bob can you check this'  (truncated to 140 chars)
   metadata       = { conversation_id: 42, message_id: 992, sender_id: 10 }
   ```
4. Publishes the notification on `notif:user:11` — Bob's existing notification WebSocket (`/ws/notifications` on the Status service, port 8515) delivers it as a `notification` event with `delivery_mode=push`.
5. Bob's chat WS (`/chat/ws` on port 8517) **also** receives the normal `message.new` event since he's a member of the conversation.

For non-mention messages: Step 3–4 happen **only when the recipient is offline** (no active chat WS connection).

---

## 11. Error Reference

All error responses share the envelope:

```json
{ "error_code": "CHAT_<CODE>", "message": "human-readable explanation" }
```

| Code | Status | Meaning |
|---|---|---|
| `CHAT_NOT_MEMBER` | 403 | Caller is not a member of the conversation |
| `CHAT_TEAM_MEMBERSHIP_REQUIRED` | 403 | Team chat post by non-member, non-Admin |
| `CHAT_ADMIN_ONLY` | 403 | Delete attempted by non-Admin |
| `CHAT_USER_INACTIVE` | 403 | DM target is disabled or soft-deleted |
| `CHAT_EDIT_NOT_OWNER` | 403 | Edit by non-sender, or non-text message |
| `CHAT_EDIT_WINDOW_EXPIRED` | 409 | Edit > 15 min after `created_at` |
| `CHAT_FORWARD_NOT_MEMBER` | 403 | Caller not in one or more forward destinations |
| `CHAT_ATTACHMENT_TOO_LARGE` | 413 | Size exceeds category cap |
| `CHAT_ATTACHMENT_TYPE_NOT_ALLOWED` | 415 | MIME not in allow-list |
| `CHAT_VOICE_DURATION_EXCEEDED` | 413 | (reserved — currently size cap also caps duration) |
| `CHAT_MESSAGE_DELETED` | 410 | Cannot operate on a soft-deleted message |
| `CHAT_NOT_FOUND` | 404 | Conversation / message / attachment missing |

Validation errors (Pydantic) come back as `422 Unprocessable Entity` with FastAPI's standard schema and **do not** wrap the chat error envelope.

Auth errors come back as `401 Unauthorized` with `{"detail": "..."}` — also not wrapped.

---

## 12. RBAC Reference

The full matrix enforced by [`chat_layer/chat_acl.py`](app/chat_layer/chat_acl.py):

| Action | Inputs | Allowed when |
|---|---|---|
| `can_post_dm` | `peer_active` | `peer_active is True` |
| `can_post_team` | `role_name`, `is_member` | `role_name ∈ {SuperAdmin, Admin}` OR `is_member is True` |
| `can_post_general` | – | always (caller already authenticated and active) |
| `can_forward_to_conversation` | `is_member_of_destination` | `is_member_of_destination is True` (no admin override) |
| `can_edit_message` | `sender_id`, `caller_id`, `created_at` | `sender_id == caller_id` AND `now - created_at ≤ 15 min` |
| `can_delete_message` | `role_name` | `role_name ∈ {SuperAdmin, Admin}` |
| `can_read_conversation` | `is_member` | `is_member is True` |

Admin role is determined from the JWT — the auth service returns `role_name` which the chat service trusts.

---

## 13. Limits & Quotas

| Limit | Value | Where enforced |
|---|---|---|
| Text message body | 4000 chars | `schemas.SendMessageRequest` |
| Image upload | 10 MB | `s3_chat_service.MAX_IMAGE_BYTES` |
| Voice upload | 10 MB | `s3_chat_service.MAX_VOICE_BYTES` |
| Voice duration (declared) | 5 min (300 s) | client-side; server stores whatever is supplied |
| File upload | 50 MB | `s3_chat_service.MAX_FILE_BYTES` |
| Forward destinations per call | 1 – 20 | `schemas.ForwardMessageRequest` |
| Search results per page | 1 – 100, default 50 | `search` endpoint |
| Message page size | 1 – 100, default 50 | `list_messages` endpoint |
| Edit window | 15 min | `chat_acl.EDIT_WINDOW` |
| Presence TTL | 90 s | `redis_chat.PRESENCE_TTL` |
| Heartbeat interval | 30 s (client) | `redis_chat.HEARTBEAT_INTERVAL` |
| Typing TTL | 5 s | `redis_chat.TYPING_TTL` |
| Pre-signed URL TTL | 3600 s (configurable) | `AWS_S3_PRESIGNED_TTL_SECONDS` env |
| URL memo lifetime | half of presigned TTL | `s3_chat_service` per-process LRU |
| Pre-signed URL memo size | 4096 entries | `s3_chat_service._URL_MEMO_MAX` |

---

## Appendix A — Quick reference card

```
REST
  GET    /chat/conversations
  POST   /chat/conversations/dm                          { peer_user_id }
  GET    /chat/conversations/general
  GET    /chat/conversations/team/{team_id}
  GET    /chat/conversations/{conversation_id}

  GET    /chat/conversations/{id}/messages?cursor=&limit=
  POST   /chat/conversations/{id}/messages               { message_type, body?, attachment_id?, reply_to_message_id? }
  PATCH  /chat/messages/{message_id}                     { body }
  DELETE /chat/messages/{message_id}                     (Admin)
  POST   /chat/messages/{message_id}/forward             { conversation_ids: [...] }
  POST   /chat/messages/{message_id}/read

  POST   /chat/attachments                               (multipart: conversation_id, duration_seconds?, file)
  GET    /chat/attachments/{id}/url

  GET    /chat/users/{user_id}/presence
  GET    /chat/search?q=&conversation_id=&limit=

  GET    /chat/health
  GET    /chat/model/api/docs                            (Swagger UI)

WS
  /chat/ws?token=<jwt>
    server → client:  message.new | message.edited | message.deleted
                       message.read | message.read_count | message.delivered
                       typing.start | typing.stop | presence.update
                       inbox.bump | unread.update | pong
    client → server:  ping | typing | mark_read

Auth
  REST:  Authorization: Bearer <jwt>
  WS:    ?token=<jwt> query string
  Cache: 60 s in Redis
```

---

*Generated 2026-04-27 to match implementation in `app/chat_layer/`. If endpoints diverge, update both this document and the code in the same PR.*
