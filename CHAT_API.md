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
| Local | `http://localhost:8050/chat` | `http://localhost:8517/chat` |
| Dev | `https://devai.api.htinfosystems.com/chat` | internal `http://chat-service:8517/chat` |
| Production | `https://<prod-host>/chat` | internal `http://chat-service:8517/chat` |

For WebSocket: `ws://<host>/chat/ws?token=<jwt>` (gateway must allow WS upgrade). On dev the WS URL is `wss://devai.api.htinfosystems.com/chat/ws?token=<jwt>` — note `wss://` because the gateway terminates TLS.

Throughout the rest of this doc, examples use the dev URL `https://devai.api.htinfosystems.com/chat`. Substitute your own host as needed.

### 1.4 How to read the curl examples

Every REST endpoint section below has a runnable **curl** block. They all share the same anatomy:

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/2/messages?limit=50' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --header 'Content-Type: application/json' \
  --data '{...request body...}'
```

| Part | Meaning |
|---|---|
| `curl --location` | The `--location` flag follows HTTP redirects automatically. Required because the API Gateway sometimes 307-redirects internally. |
| `'https://devai.api.htinfosystems.com'` | Dev environment gateway host. Matches the `gateway_url` Postman variable. |
| `/chat/...` | The Consul tag `path=/chat` registered by the chat service tells the gateway to forward this prefix. |
| `--header 'Authorization: Bearer <JWT>'` | The JWT issued by the auth service (`POST /ats/login`). The chat service validates it (cached in Redis 60 s) and rejects with 401 if missing, malformed, expired, or invalid. |
| `--header 'Content-Type: application/json'` | Required for `POST` / `PATCH` / `PUT` bodies. Skipped for `GET` and for multipart uploads (which use `multipart/form-data`). |
| `--data '...'` | The request body. JSON for most endpoints; multipart form fields for `POST /chat/attachments`. Skipped for `GET` and `DELETE`. |

**Replace `<JWT>` everywhere** with the `access_token` you got from `POST /ats/login` (or the `{{token}}` Postman variable). All examples use a placeholder `eyJhbGciOiJIUzI1NiI...` — never paste real tokens into a doc, ticket, or chat.

### 1.5 Conventions

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

#### curl

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** fetches every conversation the caller belongs to — DMs, team rooms, and `#general` — sorted with the most recent activity at the top. The caller is auto-joined to `#general` if they aren't already a member. Each row contains the data your inbox cell needs in one shot: the other party's name + avatar key (DM) or team name (team), the latest message preview, and an unread badge count.

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

#### curl

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/dm' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --header 'Content-Type: application/json' \
  --data '{
    "peer_user_id": 11
  }'
```

**What this does:** opens (or re-opens) a 1:1 DM with user `11`. The endpoint is **idempotent** — call it ten times and you get the same `conversation_id` back. Members are stored in canonical (sorted) order so `(10, 11)` and `(11, 10)` both resolve to the same row. Use the returned `id` for every subsequent send/list call.

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

#### curl

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/team/4' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** opens the chat for team `4`. If no row exists in `chat_conversations` for that team yet, the server creates one and adds every current `team_members` row as a member. On subsequent calls it reconciles membership (adds anyone newly added to the team since last access). Admins/SuperAdmins can do this for any team; regular users only for teams they're in.

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

#### curl

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/general' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** returns the singleton org-wide `#general` conversation (always `id=1`). If the caller isn't yet a member, the server inserts a row in `chat_conversation_members` for them — no admin action needed. Treat this as a "join the lobby" call; you can run it on every login as a no-op.

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

#### curl

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/42' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** fetches one specific conversation (id `42` here). Useful for a deep-link / refresh of a single chat header without touching the whole inbox. Returns 403 `CHAT_NOT_MEMBER` if the caller is not in the conversation, 404 `CHAT_NOT_FOUND` if the row was soft-deleted or never existed.

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

#### curl — first page

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/2/messages?limit=50' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** fetches the **most recent 50 messages** in conversation `2`, newest first. No cursor needed for the first page — just send the limit. The response includes `next_cursor` and `has_more`; if `has_more: true`, save the cursor for the next call.

#### curl — next page (older messages)

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/2/messages?limit=50&cursor=eyJ0IjogIjIwMjYtMDQtMjdUMTI6MDA6MzAiLCAiaSI6IDk5MH0%3D' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** fetches the next page of 50 *older* messages. The `cursor` is the **literal `next_cursor` string** from the previous response, URL-encoded (`==` becomes `%3D%3D`). The frontend never builds this — just shuttles it back. See §5.1.1 below for what the cursor encodes and why it's required for pagination beyond the first page.

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

### 5.1.1 About the `cursor`

The cursor is **server-issued** — the frontend never constructs it. It's a base64-encoded `{created_at, message_id}` of the oldest message on the page you just received. The next call sends it back so the server knows where to continue. Three rules:

1. **Omit on the first call.** Just `?limit=50`. Server returns the latest 50 + `next_cursor` for the next page.
2. **Pass back verbatim** on subsequent calls. Don't decode, don't mutate.
3. **Stop when `has_more: false`** — `next_cursor` will be `null`. Hide your "load older" button.

When `has_more: false` and you only have one message in the conversation, `next_cursor` will be `null`. **That's correct, not a bug** — there's no older page to point to.

---

### 5.2 Send a message

```http
POST /chat/conversations/{conversation_id}/messages
Authorization: Bearer <jwt>
Content-Type: application/json
```

#### curl — text message

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/2/messages' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --header 'Content-Type: application/json' \
  --data '{
    "message_type": "text",
    "body": "*hello* @bob 🎉"
  }'
```

**What this does:** sends a text message into conversation `2`. The body supports WhatsApp formatting (`*bold*`, `_italic_`, `~strike~`, `` `code` ``) which the server preserves verbatim. `@bob` is parsed server-side, looked up in `users.username`, and a high-priority `chat.mention` notification is created. Online recipients get `message.new` over the chat WS; offline ones get a row in `notifications`.

#### curl — image message (after upload)

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/2/messages' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --header 'Content-Type: application/json' \
  --data '{
    "message_type": "image",
    "body": "screenshot of the bug",
    "attachment_id": 88
  }'
```

**What this does:** sends an image message that points at attachment `88` (already uploaded via `POST /chat/attachments`). `body` is an optional caption. The response includes a fresh pre-signed `attachment.url` so the client can render the image immediately without a second round-trip.

#### curl — voice note

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/2/messages' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --header 'Content-Type: application/json' \
  --data '{
    "message_type": "voice",
    "attachment_id": 91
  }'
```

**What this does:** sends a voice note. No `body` field. The attachment row carries `duration_seconds` (set during upload) and the client-side waveform JSON.

#### curl — reply

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/conversations/2/messages' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --header 'Content-Type: application/json' \
  --data '{
    "message_type": "text",
    "body": "agreed",
    "reply_to_message_id": 991
  }'
```

**What this does:** quotes message `991` and sends a reply. The new message stores `reply_to_message_id`; the client renders the original above the reply (like WhatsApp's "swipe to reply").

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

#### curl

```bash
curl --location --request POST 'https://devai.api.htinfosystems.com/chat/messages/992/read' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** records that the caller has read message `992`. Idempotent — calling it twice is a no-op. Triggers three side-effects: writes `chat_message_reads`, advances `chat_conversation_members.last_read_message_id`, and publishes a WS event (`message.read` to the sender for DMs, `message.read_count` to all members for team/general). Also fires `unread.update` to all of *your* tabs so the badge clears across devices. Response is `204 No Content` — no body.

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

#### curl

```bash
curl --location --request PATCH 'https://devai.api.htinfosystems.com/chat/messages/992' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --header 'Content-Type: application/json' \
  --data '{
    "body": "actually, see you Friday"
  }'
```

**What this does:** edits message `992`. Three rules enforced server-side: (1) caller must be the **original sender**, (2) the message must still be `text`, (3) `now() − created_at ≤ 15 minutes`. The previous body is copied to `chat_message_edits` for audit, then the live body is overwritten and `edited_at` is set. Every conversation member gets a `message.edited` WS event so their UI re-renders the bubble in place.

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

#### curl

```bash
curl --location --request DELETE 'https://devai.api.htinfosystems.com/chat/messages/992' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** soft-deletes message `992`. Only the JWT's `role_name` ∈ `{SuperAdmin, Admin}` is allowed — non-admin senders get 403 `CHAT_ADMIN_ONLY` even on their own message. The body is retained in DB for audit; subsequent `GET .../messages` calls return `body: "[message deleted]"` and hide the attachment URL. All members get a `message.deleted` WS event so the bubble flips to the deleted state. Response is `204 No Content`.

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

#### curl

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/messages/992/forward' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --header 'Content-Type: application/json' \
  --data '{
    "conversation_ids": [17, 1, 65]
  }'
```

**What this does:** copies message `992` into conversations `17`, `1`, and `65` — three new messages are created, with the **caller** as `sender_id` and `forwarded_from_message_id=992` on each. Body and `attachment_id` are reused (the same S3 object is referenced — no duplication). The caller must be a member of every destination; the Admin team-override does **not** relax membership for forwarding. Each destination's members get a `message.new` WS event.

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

#### curl — image upload

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/attachments' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --form 'conversation_id="2"' \
  --form 'file=@"/path/to/screenshot.png"'
```

**What this does:** uploads `screenshot.png` for use in conversation `2`. Server detects the MIME (`image/png`), checks it against the allow-list and the 10 MB cap, pushes it to the `AWS_S3_BUCKET_CHAT` bucket at `chat/2/2026-04/<uuid>.png`, persists a `chat_message_attachments` row, and returns the row + a fresh pre-signed GET URL. **The file isn't visible to anyone yet** — you still need to send a message referencing the returned `id`.

#### curl — voice note upload

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/attachments' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...' \
  --form 'conversation_id="2"' \
  --form 'duration_seconds="12"' \
  --form 'file=@"/path/to/voice.webm";type=audio/webm'
```

**What this does:** same flow but for a voice note. The `duration_seconds` form field is stored in the row — the server doesn't decode the audio to verify it. The `;type=audio/webm` hint forces curl to set the right `Content-Type` part header, which the server uses for MIME-allow-list checks (browsers send this automatically; curl needs the hint).

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

#### curl

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/attachments/88/url' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** issues a fresh pre-signed GET URL for attachment `88`. Use this when the URL embedded in the original message response has expired (default TTL 3600 s). The server memoises the URL in-process for half its lifetime, so calling this back-to-back returns the same string until the memo expires.

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

#### curl

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/users/11/presence' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** fetches user `11`'s online status and last-seen timestamp. Useful for rendering "online" dots on chat headers when you don't have a live WS subscription to that user. For real-time updates, prefer the `presence.update` WS event — this REST call is for cold reads.

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

#### curl — search across all my conversations

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/search?q=hello&limit=50' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** searches `hello` across **every conversation the caller is a member of**, soft-deleted messages excluded. Uses MySQL FULLTEXT (`MATCH … AGAINST IN BOOLEAN MODE`) when the `ft_body` index is present, falling back to `LIKE %hello%`. The result has the same shape as message-list (paginated with cursor), so the same UI component can render results.

#### curl — narrow search to one conversation

```bash
curl --location 'https://devai.api.htinfosystems.com/chat/search?q=migration&conversation_id=17&limit=20' \
  --header 'Authorization: Bearer eyJhbGciOiJIUzI1NiI...'
```

**What this does:** restricts the search to conversation `17` only. The membership filter is still applied — if you pass a `conversation_id` you're not a member of, you get an empty result, **not** a 403 (the query simply joins on membership and returns zero rows). This prevents enumeration attacks.

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

A single WebSocket carries every real-time event for chat — messages, typing, presence, read receipts, and inbox updates. There is no second socket; everything multiplexes on this one channel.

### 9.1 Connect

The URL pattern (using Postman / docs variables):

```
ws://{{ws_url}}/chat/ws?token={{token}}
```

Concrete URLs by environment:

| Environment | URL |
|---|---|
| Local (direct) | `ws://localhost:8517/chat/ws?token=eyJhbGc...` |
| Local (gateway) | `ws://localhost:8050/chat/ws?token=eyJhbGc...` |
| Dev | `wss://devai.api.htinfosystems.com/chat/ws?token=eyJhbGc...` |
| Production | `wss://<prod-host>/chat/ws?token=eyJhbGc...` |

**`wss://` (TLS) on dev/prod, `ws://` (plain) only on local.** The `?token=` query parameter is the same JWT used for REST `Authorization: Bearer …`. Browsers can't set headers on a `WebSocket()` constructor, hence the query string.

#### Test from the command line

If you have `websocat` installed:

```bash
websocat 'wss://devai.api.htinfosystems.com/chat/ws?token=eyJhbGciOiJIUzI1NiI...'
```

You'll see a stream of JSON frames; type a JSON message + Enter to send (e.g. `{"action":"ping"}`).

### 9.2 Connection lifecycle

#### On successful upgrade
1. Server calls `_validate_token(token)` against the auth service (Redis-cached 60 s).
2. WS is `accept()`-ed.
3. `ws_manager.connect(ws, user_id)` adds the socket to the per-user set (multiple tabs/devices supported).
4. `chat:presence:{user_id} = online` is set in Redis (TTL **90 s**).
5. `chat_user_presence` row is upserted with `status='online'`, `last_seen_at=NOW()`.
6. A **`presence.update`** event is fanned out to every user who shares a conversation with this user. They see the green dot turn on.

#### On client disconnect (clean or unexpected)
1. `ws_manager.disconnect(ws, user_id)` removes the socket from the user's set. If it was the user's last socket, the set is dropped entirely.
2. `chat:presence:{user_id}` is deleted from Redis.
3. `chat_user_presence.status='offline'`, `last_seen_at=NOW()` is written.
4. A **`presence.update`** with `status: "offline"` is fanned out to co-conversation users.

#### On heartbeat TTL expiry (no ping for >90 s)
The Redis presence key naturally expires. The next presence read returns nothing, and the user is treated as offline. Co-conversation users **do not** receive an automatic event for this — they'll discover the offline status the next time they query `/chat/users/{id}/presence` or the next disconnect/connect cycle. (A keyspace-notification listener for proactive offline events is a follow-up.)

#### Closure codes
| Code | Reason |
|---|---|
| `4001` | Invalid or expired token (closed before `accept`) |
| `1000` | Normal close (client called `ws.close()`) |
| `1001` | Going away (page navigated away / app backgrounded) |
| `1006` | Abnormal close (network drop, server crash) |
| Other | Unexpected — client should reconnect with exponential backoff |

#### Multi-tab behaviour
A single user can have any number of concurrent WS connections (web tab + mobile webview + desktop client). Every connection receives every event for that user — that's the whole point of `unread.update` (cross-tab badge sync). Closing one tab doesn't affect the others.

---

### 9.3 The full event catalog — what fires when

This is the table to consult when you want to know **"what triggered this event?"** or conversely **"if I do X, what events fire?"**

#### Server → Client events

| Event `type` | Triggered by | Recipients | Fires once per |
|---|---|---|---|
| `message.new` | `POST /chat/conversations/{id}/messages` (send), `POST /chat/messages/{id}/forward` (forward) | Every conversation member **except** the sender | Recipient (one event per online member) |
| `message.edited` | `PATCH /chat/messages/{id}` (sender, ≤15 min) | Every conversation member | Member |
| `message.deleted` | `DELETE /chat/messages/{id}` (Admin/SuperAdmin) | Every conversation member | Member |
| `message.read` | `POST /chat/messages/{id}/read` or WS `mark_read`, **DM only** | The original **sender** of the read message | Once |
| `message.read_count` | `POST /chat/messages/{id}/read` or WS `mark_read`, **team/general only** | Every conversation member | Member |
| `message.delivered` | *(reserved — not emitted in v1)* | — | — |
| `typing.start` | WS `{action:"typing", state:"start"}` from another user | Every conversation member **except** the typer | Member |
| `typing.stop` | WS `{action:"typing", state:"stop"}` from another user | Every conversation member **except** the typer | Member |
| `presence.update` | Connection accept · disconnect · explicit close | Every user who shares ≥1 conversation with the affected user | Each co-member |
| `inbox.bump` | `POST /chat/conversations/{id}/messages` (send), `POST /chat/messages/{id}/forward` | Every conversation member **including** the sender | Member |
| `unread.update` | `POST /chat/messages/{id}/read` or WS `mark_read` (loopback) | **Every connection of the reader** (cross-tab/device sync) | Reader's connection |
| `pong` | WS `{action:"ping"}` | The pinging connection only | Per ping |

#### Client → Server actions

| Action | Effect | Server emits in response |
|---|---|---|
| `{"action":"ping"}` | Refreshes Redis presence TTL to 90 s | `{"type":"pong"}` to the same connection |
| `{"action":"typing", "conversation_id":N, "state":"start"\|"stop"}` | Sets `chat:typing:{conv}:{user}` (TTL 5 s) | `typing.start` / `typing.stop` to other conversation members |
| `{"action":"mark_read", "message_id":M}` | Writes `chat_message_reads`, advances `last_read_message_id` | DM: `message.read` to sender. Team/general: `message.read_count` to all. Always: `unread.update` to caller's other tabs |

---

### 9.4 Every server-to-client message format

Every server frame has the shape `{"type": "<name>", "data": {...}}`.

#### 9.4.1 `message.new` — a new message lands

**Trigger instance:** another conversation member just called `POST /chat/conversations/{id}/messages` or `POST /chat/messages/{id}/forward`. Sender's own connection does NOT receive this — only other members.

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

For attachment messages, `attachment` is populated identically to the REST `MessageOut` (mime, file_name, size, duration, pre-signed `url`, etc.).

#### 9.4.2 `message.edited` — body was edited within 15 min

**Trigger instance:** the original sender called `PATCH /chat/messages/{id}` within the edit window.

```json
{
  "type": "message.edited",
  "data": {
    "message_id": 992,
    "conversation_id": 42,
    "body": "actually, see you Friday",
    "edited_at": "2026-04-27T12:05:30"
  }
}
```

The client should locate the message bubble by `message_id` and replace its body in place. Render an "edited" marker.

#### 9.4.3 `message.deleted` — admin removed a message

**Trigger instance:** an Admin or SuperAdmin called `DELETE /chat/messages/{id}`.

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

Client replaces the body with a deleted-state placeholder (e.g. *"This message was deleted"*) and hides any attachment.

#### 9.4.4 `message.read` — DM blue-tick

**Trigger instance:** in a **DM**, the recipient marked your message as read (REST or WS). Sent **only to the original sender** of the read message.

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

This is the WhatsApp two-blue-ticks signal. `user_id` is the reader; `message_id` identifies which of your sent messages was read.

#### 9.4.5 `message.read_count` — group read counter

**Trigger instance:** in a **team/general** room, any member marked the message read. Sent to **every member**.

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

We send a denormalised count, not per-user reads, to keep fan-out cheap in large rooms. UIs typically render this as *"4 read"* below the message.

#### 9.4.6 `message.delivered` — *(reserved, not yet emitted)*

Schema is documented for forward-compat. Clients should ignore unknown event types gracefully, but `message.delivered` will use this shape:

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

#### 9.4.7 `typing.start` / `typing.stop`

**Trigger instance:** another conversation member sent `{"action":"typing","state":"start"|"stop"}`. The typer themselves does NOT receive these.

```json
{
  "type": "typing.start",
  "data": { "conversation_id": 42, "user_id": 11 }
}
```

```json
{
  "type": "typing.stop",
  "data": { "conversation_id": 42, "user_id": 11 }
}
```

**Client safeguard:** Redis typing key TTL is 5 s. The server doesn't push a synthetic `typing.stop` when the key expires. So the client should auto-clear its indicator after **6 seconds** of silence (5 s TTL + 1 s safety margin) even if `typing.stop` never arrives — handles the case where the typer's tab crashed mid-keystroke.

#### 9.4.8 `presence.update`

**Trigger instances** (3 cases):
- Some user X just **connected** their WS → recipients = co-members of X. Payload `status: "online"`.
- Some user X **disconnected** → recipients = co-members of X. Payload `status: "offline"`, `last_seen_at` set.
- *(Reserved)* TTL expiry, when keyspace listener is wired up. Same shape.

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

Note `last_seen_at` is `null` when `status="online"` — the user is *currently* present.

#### 9.4.9 `inbox.bump` — WhatsApp-style inbox cell update

**Trigger instance:** any new message lands in any conversation the recipient belongs to (send or forward). Sent to **every member including the sender** (the sender's row gets `unread_count: 0` so the cell still moves to top).

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

The `latest_message` shape **exactly mirrors** the same field in `GET /chat/conversations`, so a client can blindly overwrite the inbox cell's data without re-fetching the inbox. `unread_count` is recomputed from `chat_conversation_members.last_read_message_id` per-recipient, so it's authoritative even under concurrent reads.

#### 9.4.10 `unread.update` — cross-tab badge sync (loopback)

**Trigger instance:** the recipient *themself* called `POST /chat/messages/{id}/read` or sent WS `{action:"mark_read"}`. Fired on **every WS connection of that user** — desktop tab + mobile tab + browser tab in another window all clear the badge in lockstep.

```json
{
  "type": "unread.update",
  "data": {
    "conversation_id": 42,
    "unread_count": 0
  }
}
```

The connection that triggered the read also receives this — it's not de-duplicated. If your client maintains optimistic state (e.g. set `unread_count = 0` immediately on `mark_read`), the loopback is a no-op confirmation.

#### 9.4.11 `mention` — *(reserved on chat WS)*

In v1, mention pushes go through the **existing notification WebSocket** (`/ws/notifications` on the Status service, port 8515) as a `chat.mention` event. The chat WS does not emit a separate `mention` event today. Listening only on `/chat/ws`? You still get `message.new` with `mentions: [your_user_id]` in the payload — that's enough to highlight.

#### 9.4.12 `pong` — heartbeat ack

**Trigger instance:** client sent `{"action":"ping"}`. Sent only to the pinging connection.

```json
{ "type": "pong" }
```

No `data` field — the type alone is the signal. Used purely for liveness; no business meaning.

---

### 9.5 Every client-to-server message format

All sent as **text frames** (JSON-encoded strings). Binary frames are not accepted.

#### 9.5.1 `ping` — keep presence alive

**When to send:** every **30 seconds** while the WS is open. Redis TTL is 90 s, so missing 3 pings drops you offline.

```json
{ "action": "ping" }
```

Server replies `{"type":"pong"}` to the same connection. Refreshes `chat:presence:{user_id}` TTL to 90 s.

#### 9.5.2 `typing` — start/stop indicator

**When to send:**
- `start` on the first keystroke after a 3-second pause
- `stop` on send-message OR after 4 seconds of typing inactivity (whichever comes first)

```json
{
  "action": "typing",
  "conversation_id": 42,
  "state": "start"
}
```

```json
{
  "action": "typing",
  "conversation_id": 42,
  "state": "stop"
}
```

Server writes `chat:typing:{conv}:{user}` to Redis with TTL 5 s and fans out `typing.start` / `typing.stop` to other conversation members.

#### 9.5.3 `mark_read` — read a message

**When to send:** when a message becomes visible in the viewport (intersection observer) OR the user manually marks all read.

```json
{
  "action": "mark_read",
  "message_id": 991
}
```

Equivalent to `POST /chat/messages/991/read` but cheaper (no HTTP round-trip, no JWT re-validation per call). Server writes `chat_message_reads`, advances `last_read_message_id`, and emits the appropriate read events plus a `unread.update` loopback.

#### Frames the server will silently ignore

To keep the protocol stable, the server **drops** (no error, no close) any frame that:
- Is not valid JSON
- Has no `action` field
- Has an unknown `action` value
- Is a binary frame

Don't rely on this — but you can `JSON.stringify` confidently knowing a typo won't kill the connection.

---

### 9.6 Reference client implementation

```javascript
const WS_URL = `wss://devai.api.htinfosystems.com/chat/ws?token=${jwt}`;

let ws, pingTimer, retryCount = 0;

function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    retryCount = 0;
    // 30-second heartbeat (server TTL is 90 s)
    pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: "ping" }));
      }
    }, 30_000);
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
      case "message.new":         onMessageNew(msg.data); break;
      case "message.edited":      onMessageEdited(msg.data); break;
      case "message.deleted":     onMessageDeleted(msg.data); break;
      case "message.read":        onDmRead(msg.data); break;       // DM blue tick
      case "message.read_count":  onGroupReadCount(msg.data); break;
      case "typing.start":        onTypingStart(msg.data); break;
      case "typing.stop":         onTypingStop(msg.data); break;
      case "presence.update":     onPresenceUpdate(msg.data); break;
      case "inbox.bump":          onInboxBump(msg.data); break;    // re-render inbox cell
      case "unread.update":       onUnreadUpdate(msg.data); break; // cross-tab badge clear
      case "pong":                /* heartbeat ack — ignore */ break;
      default:                    console.warn("unknown event", msg.type);
    }
  };

  ws.onclose = (e) => {
    clearInterval(pingTimer);
    if (e.code === 4001) {
      // Invalid token — re-login required, do NOT auto-reconnect
      window.location = "/login";
      return;
    }
    if (e.code !== 1000) {
      // Exponential backoff, capped at 30 s
      const delay = Math.min(30_000, 1000 * 2 ** retryCount++);
      setTimeout(connect, delay);
    }
  };

  ws.onerror = (err) => console.error("WS error", err);
}

function sendTyping(conversationId, state) {
  ws.send(JSON.stringify({ action: "typing", conversation_id: conversationId, state }));
}

function markRead(messageId) {
  ws.send(JSON.stringify({ action: "mark_read", message_id: messageId }));
}

connect();
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

### 10.1 Two users chatting (full event sequence)

```
Alice                                Server                                Bob
  │                                    │                                    │
  │ WSS /chat/ws?token=<jwt>           │       WSS /chat/ws?token=<jwt>     │
  ├───────────────────────────────────►│◄───────────────────────────────────┤
  │                                    │ presence.update {alice online} ──►│
  │◄── presence.update {bob online}    │                                    │
  │                                    │                                    │
  │ POST /chat/conversations/dm        │                                    │
  │ { peer_user_id: 11 }               │                                    │
  ├───────────────────────────────────►│                                    │
  │◄── 200 { id: 42, type: "dm", ... } │                                    │
  │                                    │                                    │
  │ POST /chat/conversations/42/messages                                    │
  │ { message_type: "text", body: "hi" }│                                    │
  ├───────────────────────────────────►│                                    │
  │◄── 200 MessageOut(id=992)          │ ── message.new {msg 992} ────────►│
  │                                    │ ── inbox.bump {conv 42, unr=1} ──►│
  │ ◄── inbox.bump {conv 42, unr=0}    │  (sender's own bump, unread=0)     │
  │                                    │                                    │
  │ {action:"ping"} every 30s ───────► │ ◄── {action:"ping"} every 30s     │
  │ ◄── pong                           │ pong ─────────────────────────────►│
  │                                    │                                    │
  │                                    │ ◄── {action:"mark_read", id:992}   │
  │                                    │ ── unread.update {conv 42, unr=0}►│ (cross-tab)
  │ ◄── message.read {msg 992 by bob}  │                                    │
```

Every event you see above is documented in §9.4. `inbox.bump` updates the inbox cell preview + badge for both parties. `unread.update` clears the badge across all of Bob's connected tabs/devices.

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
