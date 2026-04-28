# Chat Tester (React UI)

Self-contained single-page React app for exercising every chat REST endpoint and the WebSocket against the dev gateway. No build step.

## Run

### ⭐ Recommended — use the bundled proxy (zero CORS issues)

```bash
cd "d:/Recruitment Agent/AI_AGENT15_STATUS_SERVICE/chat_test_ui"
uv run --no-project python serve.py
```

(or `python serve.py` if uv is not in your PATH)

Then open **http://localhost:5173**.

What `serve.py` does:
- Serves `index.html` on `http://localhost:5173`
- **Proxies** every `/auth/*` and `/chat/*` request to `https://devai.api.htinfosystems.com`
- Proxies the `/chat/ws` WebSocket too

The browser sees one origin (`http://localhost:5173`) for everything — the page, the auth API, the chat API, and the WebSocket. **No cross-origin requests, so no CORS errors are even possible.**

After login, the page shows a `↪ use this origin` link near the gateway URL field. Click it once to flip the gateway to `http://localhost:5173`. The session is persisted to `sessionStorage` so refresh keeps you logged in.

### Option 2 — plain static server (simpler, may hit CORS)

```bash
cd chat_test_ui
python -m http.server 5173
```

Open `http://localhost:5173`. The page is served from localhost but API calls go directly to the dev gateway — works **only if** the auth + chat services accept your origin in their `Access-Control-Allow-Origin` header. If you see CORS errors, fall back to the proxy in Option 1.

### Option 3 — open the file directly

```
double-click chat_test_ui/index.html
```

Origin will be `null` (file://). Most servers reject this. Only useful if both services have `Access-Control-Allow-Origin: *` AND don't set `allow_credentials=true`. Likely to fail; use Option 1 instead.

### Option 4 — bundle into the existing `notification_ui`

Drop `index.html` into `notification_ui/` and access via the same uvicorn that already serves it on port 5009. Useful for showing the tester to other devs without them needing local checkout.

## Login

Defaults are pre-filled with `suphti` / `hti@123` against `https://devai.api.htinfosystems.com`. Hit **Login** — the auth service is called via:

```
POST /auth/ats/login?username=...&password=...
```

The returned `access_token` is stored in `sessionStorage` so a refresh keeps you logged in. **Logout** clears it.

## What you can do

### Inbox (left sidebar)
- Lists every conversation you belong to, sorted newest-first
- Shows peer name (DM), team name, or `#general`
- Live-updates from `inbox.bump` events — a new message moves the conversation to top with a badge tick
- Click a row to open the conversation
- "+ New DM" — type a peer user id to open/create a 1:1
- "+ Open Team" — type a team id to open/create the team chat
- "# general" — jumps to `#general`
- "↻ Refresh" — re-fetches the inbox

### Conversation pane (center)
- Header shows the title and presence dot for DMs
- Messages render with type-specific UI:
  - **Text:** body + edit/delete/reply/forward controls
  - **Image:** inline `<img>` from the pre-signed URL
  - **Voice:** inline `<audio controls>`
  - **File:** download link
- "Reply" — a quote bar appears above the composer; the next send carries `reply_to_message_id`
- "Forward" — opens a checkbox list of your other conversations; multi-select + send
- "Edit" — only on your own text messages, only within 15 min (server enforces; UI just shows the button)
- "Delete" — only visible if `role_name` is admin/super_admin
- "Mark read" — manual override (the latest message is auto-marked when you scroll into view)
- "Load older" — pages backwards using the cursor
- "Auto mark-read on view" — toggle controlling the auto behavior

### Composer
- Plain text with WhatsApp-style markers (`*bold*`, `_italic_`, `~strike~`, `` `code` ``, `@username` mentions)
- Enter to send, Shift+Enter for newline
- 📎 button — uploads any image / audio / pdf / doc / zip and immediately sends a message of the right type
- Typing indicator: starts on first keystroke, auto-stops after 4 s of inactivity or on send

### Right panel — three tabs
1. **WS Events** — live JSON dump of every server → client frame, with filter box and a "ping" button
2. **REST** — every fetch call with method, path, status, duration, response body (truncated)
3. **Tools** —
   - Full-text search across all your conversations
   - Presence lookup by user id
   - Raw WS frame sender (paste any JSON, send as a text frame)

## Known limitations

- No native voice recorder — pick an existing `audio/webm` file to test voice notes. (Browser MediaRecorder integration is a follow-up.)
- No image preview before send — the file is uploaded immediately when picked.
- Search results are flat (no jump-to-message) — manual scroll only.
- The "auto mark-read" only fires for the latest message, not every visible message.

## Auth flow

```
[Browser]                                [Auth Service]
   │ POST /auth/ats/login?username=…       │
   ├──────────────────────────────────────►│
   │ ◄── { access_token, user_id, role_name, … }
   │
   │ stores in sessionStorage
   │
   │ wss://…/chat/ws?token=<access_token>  [Chat Service]
   ├──────────────────────────────────────►│
   │                                       │ validates JWT (Redis-cached 60s)
   │ ◄── presence.update + inbox.bump …    │
```

The same JWT is used for REST (`Authorization: Bearer …`) and WebSocket (`?token=…` query string). On 401 / WS code 4001 you're redirected back to the login screen.

## Troubleshooting

| Symptom | Check |
|---|---|
| Login button does nothing | Open DevTools → Network. Is the OPTIONS preflight failing? Auth service must allow your origin. |
| WS shows "auth-failed" | Token is invalid/expired. Logout + re-login. |
| WS keeps disconnecting | Check Network → WS tab for the close code. `1006` usually means server crashed or proxy timed out. |
| Inbox empty | You haven't been added to any conversations yet. Try `+ New DM` with a known user id, or `# general`. |
| Forward button errors with `CHAT_FORWARD_NOT_MEMBER` | You picked a destination conversation you're not a member of. Pick from the rendered list — it shouldn't include those, but double-check. |
| Delete button doesn't appear on your own message | `role_name` from your JWT isn't admin/super_admin. Ask whoever provisioned the user to bump the role. |
| Voice file upload fails with 415 | Browser couldn't determine the MIME type. Use Chrome and pick a `.webm` / `.mp3` / `.m4a` file. |
