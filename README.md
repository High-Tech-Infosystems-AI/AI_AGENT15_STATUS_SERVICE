# AI_AGENT15_STATUS_SERVICE

Status Service API for tracking Celery task progress via WebSocket connections.

## Prerequisites

- Python >= 3.13
- UV package manager
- Redis server (for Celery backend)
- MySQL database

## Installation

1. Install dependencies using UV:
```bash
uv sync
```

## Configuration

Create a `.env` file in the root directory with the following variables:

```env
# Database Configuration
DB_HOST=localhost
DB_PORT=3306
DB_NAME=ats_main
DB_USER=root
DB_PASSWORD=your_password

# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=

# Logging
STATUS_AGENT_LOG=D:\LOGS

# File Storage
FILE_STORING_PATH=
```

## Running the Application

### Development Mode

Run the application using UV and Uvicorn:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8116 --reload
```

### Production Mode

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### With Custom Host and Port

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## API Documentation

Once the server is running, access the interactive API documentation at:
- Swagger UI: `http://localhost:8000/model/api/docs`
- ReDoc: `http://localhost:8000/redoc`

## WebSocket Endpoint

### Connect to Task Progress Updates

**Endpoint**: `ws://localhost:8000/ws/tasks/{task_id}`

**Example**:
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/tasks/abc123-def456-ghi789');

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log('Task Status:', data.status);
    console.log('Progress:', data.progress);
    console.log('Message:', data.message);
    
    if (data.status === 'SUCCESS' && data.jd) {
        console.log('JD Content:', data.jd);
    }
    
    if (data.status === 'FAILED' && data.error) {
        console.error('Error:', data.error);
    }
};
```

### Response Format

```json
{
    "taskId": "abc123-def456-ghi789",
    "status": "IN_PROGRESS",
    "progress": 50,
    "message": "Task in progress"
}
```

**Status Values**:
- `PENDING`: Task is waiting to start
- `IN_PROGRESS`: Task is currently running
- `SUCCESS`: Task completed successfully (includes `jd` field)
- `FAILED`: Task failed (includes `error` field)
- `ERROR`: Error occurred while fetching progress

## Features

- Real-time task progress tracking via WebSocket
- Redis integration for Celery backend (DB 1)
- Automatic connection management
- Error handling and logging
- CORS support for cross-origin requests

## Project Structure

```
app/
├── api/
│   ├── endpoints/
│   │   ├── dependencies/
│   │   │   └── progress.py      # Redis progress tracking
│   │   └── websocket_tasks.py   # WebSocket endpoint
│   └── status_api.py            # Main API router
├── cache_db/
│   └── redis_config.py          # Redis configuration
├── core/
│   ├── config_dev.py            # Development settings
│   ├── config_prod.py           # Production settings
│   └── logger.py                # Logging configuration
├── database_Layer/
│   ├── db_config.py             # Database configuration
│   ├── db_model.py              # SQLAlchemy models
│   ├── db_schema.py             # Database schemas
│   └── db_store.py              # Database store
└── main.py                      # FastAPI application entry point
```

## Environment Variables

The application uses environment-based configuration:
- Development: Set `APP_ENV=dev` (default)
- Production: Set `APP_ENV=prod`

## Notes

- The WebSocket polls Redis every 2 seconds for task updates
- Connection automatically closes when task status is `DONE` or `FAILED`
- Redis DB 0 is used for Celery message broker
- Redis DB 1 is used for Celery backend (task results)

## Chat Module

The chat module lives in `app/chat_layer/` and **runs as its own FastAPI process**
(`app.chat_main:app`) on **port 8517** — separate from the Status/Notification API on 8515.
It registers with Consul as `HRMIS_CHAT_SERVICE` with tag `path=/chat`, so the API Gateway
discovers and routes `/chat/*` automatically without any gateway-side configuration.

### Topology
```
┌──────────────────────────────── Container ────────────────────────────────┐
│                                                                           │
│   app.main:app           app.chat_main:app          notification_ui       │
│   port 8515              port 8517                  port 5009             │
│   /status/*              /chat/* + /chat/ws         (web UI)              │
│   /health                /chat/health                                     │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
        │                          │
        ▼                          ▼
  Consul: HRMIS_STATUS_SERVICE   Consul: HRMIS_CHAT_SERVICE
  tags: path=/status             tags: path=/chat
        │                          │
        └─────────── API Gateway ───┘
                  (routes by Consul `path=` tag)
```

### Setup
- Apply migrations v8–v13 in `migrations/` (in order).
- Set env vars:
  - `CHAT_SERVICE_PORT` (default `8517`)
  - `CHAT_SERVICE_NAME` (default `HRMIS_CHAT_SERVICE`)
  - `CHAT_SERVICE_PATH` (default `/chat`)
  - `CHAT_SERVICE_AUTH` (default `mixed`)
  - `AWS_S3_BUCKET_CHAT` (required for attachments)
  - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`
  - `AWS_S3_PRESIGNED_TTL_SECONDS` (default 3600)

### Run
- **Locally (chat only):** `uv run uvicorn app.chat_main:app --port 8517 --reload`
- **Locally (everything):** `bash start.sh`
- **Docker:** `docker build -t status-chat .` + `docker run -p 8515:8515 -p 8517:8517 -p 5009:5009 ...`
- **k8s:** apply [k8s/deployment.yaml](k8s/deployment.yaml) — exposes both ports from the same pod.

### RBAC
- DM: any active user ↔ any active user.
- Team chat: SuperAdmin/Admin → any team; others → only member teams.
- `#general`: all active users.
- Delete: SuperAdmin/Admin only (soft-delete).

### Endpoints
- `GET /chat/conversations` — list mine
- `POST /chat/conversations/dm` — create or get a DM
- `GET /chat/conversations/team/{team_id}` — lazy-create team conversation
- `GET /chat/conversations/general` — singleton `#general`
- `GET /chat/conversations/{id}` — get by id
- `GET /chat/conversations/{id}/messages?cursor=&limit=` — paginated history
- `POST /chat/conversations/{id}/messages` — send message
- `PATCH /chat/messages/{id}` — edit own message within 15 min
- `DELETE /chat/messages/{id}` — admin delete (soft)
- `POST /chat/messages/{id}/forward` — forward to other conversations
- `POST /chat/messages/{id}/read` — mark message read
- `POST /chat/attachments` — upload (multipart) to S3
- `GET /chat/attachments/{id}/url` — pre-signed GET
- `GET /chat/users/{id}/presence` — presence + last-seen
- `GET /chat/search?q=&conversation_id=` — search
- `WS /chat/ws?token=` — real-time stream

### Tests
`uv run pytest tests/chat -v`
