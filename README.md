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
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
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
