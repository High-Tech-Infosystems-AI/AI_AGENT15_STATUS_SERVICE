# Chat Service — Testing Walkthrough

End-to-end flow for testing **DM creation → send → receive → WebSocket events** using the [chat-service.postman_collection.json](chat-service.postman_collection.json).

You'll need **two users** (Alice + Bob) so you can watch the WebSocket actually deliver messages from one to the other.

---

## Prerequisites

- Chat service running on port **8517** (`uv run uvicorn app.chat_main:app --port 8517` or `bash start.sh`)
- Status service running on port **8515** (the chat service depends on its auth token cache + notification bridge)
- API Gateway running and registered with Consul (or skip the gateway — see "Bypass" note below)
- Two valid user accounts in `users` (both with `enable=1`, both not soft-deleted) — say **Alice** (`user_id=10`) and **Bob** (`user_id=11`)
- Migrations v8–v13 applied to MySQL
- Redis running
- `AWS_S3_BUCKET_CHAT` configured (only needed if you'll test attachments — skip for the basic DM flow)

> **Bypass the gateway during dev:** in Postman, change `{{gateway_url}}` to `{{chat_direct_url}}` (default `http://localhost:8517`) for any request. The runbook below assumes gateway routing — substitute as needed.

---

## Setup: import the collection and prepare two contexts

1. **Import** [chat-service.postman_collection.json](chat-service.postman_collection.json) into Postman.

2. You need two independent token sets, one per user. Two clean ways:

   **Option A — two Postman environments** (recommended):
   - Create environment `Alice`: variables `token`, `user_id`, `peer_user_id` (set `peer_user_id=11`).
   - Create environment `Bob`:   variables `token`, `user_id`, `peer_user_id` (set `peer_user_id=10`).
   - Switch environments using the dropdown in the top-right of Postman.

   **Option B — duplicate the collection** as `Chat Service (Alice)` and `Chat Service (Bob)`. Each duplicate has its own collection variables. Less elegant but works without environment juggling.

3. Confirm `gateway_url` (e.g. `http://localhost:8050`), `chat_direct_url` (`http://localhost:8517`), `auth_url` (`http://localhost:8085`), and `ws_url` (`ws://localhost:8517` or your gateway WS URL) point at your environment.

---

## Phase 1 — Both users log in

Run as **Alice** (env `Alice` selected):

| Step | Action | Verify |
|---|---|---|
| 1.1 | Open `0. Auth → Login`. Edit body to Alice's `username` / `password`. Hit Send. | Status `200`. Console shows `Login OK. user_id=10`. The collection variable `token` is now set. |
| 1.2 | Confirm in Postman → bottom right → Environment quick-look that `token` and `user_id` are populated. | Both have values. |

Switch environment to **Bob**, repeat with Bob's credentials. After this step both Alice's and Bob's tokens are saved in their respective environments.

---

## Phase 2 — Open Bob's WebSocket FIRST (so he can receive)

You want Bob already connected when Alice sends, so the message arrives in real time instead of as an offline notification.

| Step | Action | Verify |
|---|---|---|
| 2.1 | Switch environment to **Bob**. | Active env shown top-right is `Bob`. |
| 2.2 | Open `6. WebSocket → Connect to /chat/ws`. Click **Connect**. | Status pill turns green: `WebSocket Connected`. The "Messages" pane is empty but ready. |
| 2.3 | Watch the messages pane. You should immediately see a `presence.update` event for Bob himself (status=`online`) being broadcast — and any other co-conversation users will see it too. | A frame appears with `{"type":"presence.update","data":{"user_id":11,"status":"online", ...}}` (server → client direction). |

Keep this Postman tab open and visible. **Don't close it** — every step from here on out will produce events on this socket.

---

## Phase 3 — Alice creates a DM with Bob

In a **separate Postman tab/window**, switch to env **Alice**.

| Step | Action | Verify |
|---|---|---|
| 3.1 | Open `1. Conversations → List my conversations`. Send. | `200`. Response is an array; if Alice has no prior chats it's just `#general`. Console logs the auto-saved `conversation_id`. |
| 3.2 | Open `1. Conversations → Create or get DM`. Confirm body is `{"peer_user_id": 11}`. Send. | `200`. Response is a single conversation object with `type=dm` and `members=[10, 11]`. The script auto-saves `conversation_id` (let's say `42`). |
| 3.3 | Re-run `List my conversations`. | The new DM is now in the list. `peer = {id:11, name:"Bob ...", ...}` is populated. `latest_message` is `null` because we haven't sent anything yet. `unread_count` is `0`. |

> **What just happened on Bob's WS?** Nothing yet — DM creation isn't broadcast. Both users have to send a message before any WS event fires. We'll do that next.

---

## Phase 4 — Alice sends a text message; Bob's WebSocket receives it

This is the moneyshot — the moment that proves the whole pipeline works.

| Step | Action (Alice tab) | What Bob's WebSocket should display |
|---|---|---|
| 4.1 | Open `2. Messages → Send text message`. Body is `{"message_type":"text","body":"hi @bob"}`. Send. | A WS frame appears (server → client) with `type: "message.new"` and the full message payload. |
| 4.2 | Same Alice tab — `200 OK`. The script saves `message_id` (let's say `992`). | A second WS frame: `type: "inbox.bump"`, `data: {"conversation_id": 42, "unread_count": 1, "latest_message": {...}}`. This is the WhatsApp-style inbox cell update. |
| 4.3 | Bob also gets a third frame **on the notification WebSocket** (port 8515, `/ws/notifications`) — a `chat.mention` push because Alice tagged him with `@bob`. | If you have a notification WS open too (separate Postman request), you'll see it. Otherwise check `notifications` table for a row with `domain_type='chat', event_type='chat.mention'`. |

**If Bob's WS shows nothing:**
- Confirm Bob's connection is still green (the heartbeat must be running — see Phase 8).
- Confirm Bob is actually a member of conversation `42`. Run `1. Conversations → Get by id` from Bob's tab — it should `200`. If `403 CHAT_NOT_MEMBER`, the DM creation didn't reach Bob's row (rare; check the unique constraint and that both user IDs are correct).
- Confirm Redis Pub/Sub is reachable. The chat service logs include `Chat Redis subscriber started`. If you don't see that line on startup, the WS subscriber never came up and no real-time delivery is happening.

---

## Phase 5 — Bob receives history and reads it

Switch to Bob's tab (the one running the WebSocket), or open a new Postman tab in env **Bob**.

| Step | Action | Verify |
|---|---|---|
| 5.1 | Open `1. Conversations → List my conversations` from Bob's env. | The DM with Alice is at the top, `peer.name = "Alice ..."`, `unread_count = 1`, `latest_message.body_preview = "hi @bob"`. |
| 5.2 | Open `2. Messages → List messages (paginated)`. Confirm `conversation_id` is `42` (the DM). Send. | `200`. `items[0].body == "hi @bob"`. `has_more` likely `false` for one message. |
| 5.3 | Now mark it read. Two ways — pick either: **(A) REST**: `2. Messages → Mark message read` → Send. **(B) WebSocket**: in Bob's WS tab, send the frame from `6. WebSocket → Sample: mark_read`. | Both update `chat_message_reads` and `chat_conversation_members.last_read_message_id`. |

**On Alice's WebSocket** (open `Connect to /chat/ws` in a third tab as Alice if you haven't yet):
- A frame `type: "message.read"`, `data: {"message_id": 992, "user_id": 11, "read_at": "..."}` arrives. This is the DM blue-tick — Bob has read Alice's message.

**On Bob's WebSocket** (cross-tab badge clear):
- A frame `type: "unread.update"`, `data: {"conversation_id": 42, "unread_count": 0}` arrives. Even though Bob's only got one tab open right now, the loopback fires regardless — try this with two browsers/devices to see the multi-tab effect.

---

## Phase 6 — Typing indicator round-trip

Verifies typing fan-out works.

| Step | Action | Verify |
|---|---|---|
| 6.1 | In **Alice's** WS tab, send the `Sample: typing.start` frame: `{"action":"typing","conversation_id":42,"state":"start"}`. | Nothing back to Alice. |
| 6.2 | **Bob's** WS receives a frame: `{"type":"typing.start","data":{"conversation_id":42,"user_id":10}}`. | Verified. |
| 6.3 | Wait 6 seconds without sending `typing.stop`. | Bob's UI should auto-clear (Redis typing key TTL is 5 s). The server doesn't push a `typing.stop` automatically on TTL expiry — the client just times out. |
| 6.4 | Or send `Sample: typing.stop` from Alice. | Bob's WS gets `{"type":"typing.stop","data":{"conversation_id":42,"user_id":10}}`. |

---

## Phase 7 — Edit and delete

| Step | Action | Verify |
|---|---|---|
| 7.1 | As Alice, open `2. Messages → Edit message`. Body `{"body":"hi @bob (edited)"}`. Send within 15 min of the original. | `200`. Bob's WS receives `message.edited` event with the new body. Beyond 15 min you get `409 CHAT_EDIT_WINDOW_EXPIRED`. |
| 7.2 | As **Admin/SuperAdmin** (you may need to log in as a different user with role `Admin`), open `2. Messages → Delete message` for `message_id=992`. Send. | `204`. Both Alice's and Bob's WSs receive `message.deleted`. Subsequent fetches of message 992 return `body = "[message deleted]"`. |
| 7.3 | If you call delete as a non-Admin: | `403 CHAT_ADMIN_ONLY`. |

---

## Phase 8 — Heartbeat + presence transition

Demonstrates the offline transition that drives last-seen.

| Step | Action | Verify |
|---|---|---|
| 8.1 | While Bob's WS is connected, send `Sample: ping` every 30 s. Server replies `{"type":"pong"}`. | Pongs come back. The Redis presence key is being refreshed; Bob stays `online`. |
| 8.2 | Stop sending pings. Wait **>90 seconds**. | The Redis key expires. The server should detect this on TTL expiry. |
| 8.3 | From any other user's tab, hit `4. Presence → Get user presence` for `peer_user_id=11`. | Status is `offline`. `last_seen_at` is set to the moment Bob disconnected. |
| 8.4 | Or: **just close Bob's WS**. | Server immediately writes `chat_user_presence.status='offline'`, `last_seen_at=NOW()`, and fans out a `presence.update` to every co-conversation user. Alice's WS should see it. |

> The 90 s presence TTL is in [redis_chat.py](app/chat_layer/redis_chat.py) `PRESENCE_TTL`. Heartbeat interval is documented as 30 s but enforced by the *client* — the server just refreshes the TTL whenever it gets a `ping`.

---

## Phase 9 — Attachments (optional — only if AWS_S3_BUCKET_CHAT is set)

| Step | Action | Verify |
|---|---|---|
| 9.1 | Switch to Alice. Open `3. Attachments → Upload attachment`. In the body, set `conversation_id={{conversation_id}}` and pick a small JPEG / PNG. Send. | `200` with `id`, `mime_type`, `s3_key`, `url` (pre-signed GET). The script auto-saves `attachment_id`. |
| 9.2 | Open `2. Messages → Send image message`. Body uses `{{attachment_id}}`. Send. | `200`. Bob's WS receives `message.new` with the full message including `attachment.url`. |
| 9.3 | Click the URL in Bob's response — the image should load. | Image renders. URL is signed — copy it after the `?` and you can see `X-Amz-Signature`. |
| 9.4 | After the URL TTL (default 1 hour), refresh with `3. Attachments → Get fresh pre-signed URL`. | `200`. New URL with a new signature. |

For voice notes: same flow but `audio/webm` file, and pass `duration_seconds=12` in the upload form. Then `Send voice message` references the `attachment_id`.

---

## Phase 10 — Forward + reply

| Step | Action | Verify |
|---|---|---|
| 10.1 | As Alice, open `2. Messages → Reply to a message`. Body uses `reply_to_message_id={{message_id}}`. Send. | `200`. New message has `reply_to_message_id` set; Bob's WS receives `message.new` with the same. Clients render the original message as a quoted block. |
| 10.2 | As Alice, open `2. Messages → Forward message`. Body `{"conversation_ids":[1, 17]}` (1 = `#general`, 17 = a team you're in). Send. | `200` with an array of new messages, one per destination. Each carries `forwarded_from_message_id` pointing at the original. |
| 10.3 | Try forwarding into a conversation Alice is **not** a member of. | `403 CHAT_FORWARD_NOT_MEMBER`. |

---

## Phase 11 — Search

| Step | Action | Verify |
|---|---|---|
| 11.1 | Open `5. Search → Search messages` with `q=hello`. Send. | `200`. Returns messages from any conversation Alice is a member of, soft-deleted excluded. |
| 11.2 | Search for a unique word from your earlier test message. | The message appears. |
| 11.3 | Add `conversation_id=42` query param. | Results narrow to that conversation only. |

---

## Quick troubleshooting cheat sheet

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on every chat call | Token expired or missing | Re-run `0. Auth → Login`; check `{{token}}` is populated. |
| `403 CHAT_NOT_MEMBER` | The conversation row doesn't have a `chat_conversation_members` entry for the caller | For DMs, the create/get-DM endpoint should have inserted both. For team chats, run the team endpoint to lazy-create. |
| `403 CHAT_TEAM_MEMBERSHIP_REQUIRED` | Caller isn't in `team_members` for that team and isn't Admin/SuperAdmin | Add via RBAC, or impersonate Admin. |
| `404 CHAT_NOT_FOUND` on a recently created conversation | Wrong `conversation_id` variable | Re-run `List my conversations` to refresh the auto-saved variable. |
| `413 CHAT_ATTACHMENT_TOO_LARGE` | File exceeds the category cap | Image/voice 10 MB, file 50 MB. Re-encode or pick smaller. |
| `415 CHAT_ATTACHMENT_TYPE_NOT_ALLOWED` | MIME outside the allow-list | See [s3_chat_service.py](app/chat_layer/s3_chat_service.py) `ALLOWED_*_MIMES`. |
| WS closes immediately with code `4001` | Token invalid/expired | Re-login and reconnect. Check `AUTH_SERVICE_URL` is reachable from the chat service. |
| WS connects but receives nothing | Chat Redis subscriber not running | Check chat service startup logs for `Chat Redis subscriber started`. If missing, Redis is unreachable. |
| `message.new` fires for the sender too | It shouldn't — sender is excluded in the fan-out loop | If you're seeing this, you're probably listening on two tabs as the same user. The `inbox.bump` event IS sent to the sender (with `unread_count=0`), and that's intentional — for moving the cell to the top of their own inbox. |
| `unread_count` never decrements after reading | `mark_read` writes `chat_message_reads` but `update_last_read` failed silently | Check the message id you're marking exists in `chat_messages`. The `last_read_message_id` only moves forward; older message ids are no-ops. |
| Gateway returns `503 Service Unavailable` for `/chat/*` | Chat service didn't register with Consul, or its health check is failing | `consul members`; check Consul UI for `HRMIS_CHAT_SERVICE`; curl `/chat/health` directly on port 8517. |

---

## Minimal happy-path sequence (copy-paste TL;DR)

```
[Tab A — Bob]
1. Login (env=Bob)
2. WebSocket → Connect to /chat/ws

[Tab B — Alice]
3. Login (env=Alice)
4. Conversations → Create or get DM   (peer_user_id=11)
5. Messages → Send text message       ("hi @bob")

[Tab A — Bob, watching the WS]
   ✓ message.new arrives
   ✓ inbox.bump arrives (unread_count=1)

[Tab A — Bob]
6. Send WS frame {action:"mark_read", message_id:<id>}

[Tab C — Alice WS]
   ✓ message.read arrives  (DM blue-tick)

[Tab A — Bob WS]
   ✓ unread.update arrives (badge cleared on other tabs)
```

That's the canonical proof — anything more is just exercising the additional surface.
