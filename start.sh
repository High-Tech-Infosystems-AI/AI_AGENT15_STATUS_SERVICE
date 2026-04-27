#!/bin/bash
# Start three processes in this container:
#   1. Status / Notification API   (port 8515 — app.main:app)
#   2. Chat API                    (port 8517 — app.chat_main:app)
#   3. Notification UI             (port 5009 — notification_ui/server.py)
#
# Celery Beat is optional; run separately if needed:
#   celery -A app.notification_layer.celery_app worker --loglevel=info -Q notification &
#   celery -A app.notification_layer.celery_app beat --loglevel=info &
# The asyncio fallback scheduler handles everything when Celery is not running.

STATUS_PORT="${STATUS_SERVICE_PORT:-8515}"
CHAT_PORT="${CHAT_SERVICE_PORT:-8517}"
UI_PORT="${UI_PORT:-5009}"

echo "Starting Status + Notification API on port ${STATUS_PORT}..."
uvicorn app.main:app --host 0.0.0.0 --port "${STATUS_PORT}" &
API_PID=$!

echo "Starting Chat API on port ${CHAT_PORT}..."
uvicorn app.chat_main:app --host 0.0.0.0 --port "${CHAT_PORT}" &
CHAT_PID=$!

echo "Starting Notification UI on port ${UI_PORT}..."
cd /app/notification_ui
uvicorn server:app --host 0.0.0.0 --port "${UI_PORT}" &
UI_PID=$!
cd /app

echo "All services started (API=${API_PID}, CHAT=${CHAT_PID}, UI=${UI_PID})"

# Wait for any to exit; if one dies, kill the rest and exit
wait -n "${API_PID}" "${CHAT_PID}" "${UI_PID}"
EXIT_CODE=$?

echo "A process exited with code ${EXIT_CODE}. Stopping all..."
kill "${API_PID}" "${CHAT_PID}" "${UI_PID}" 2>/dev/null
exit "${EXIT_CODE}"
