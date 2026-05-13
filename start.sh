#!/bin/bash
# Start four processes in this container:
#   1. Status / Notification API   (port 8515 — app.main:app)
#   2. Chat API                    (port 8517 — app.chat_main:app)
#   3. AI Chat API                 (port 8518 — app.ai_chat_main:app)
#   4. Notification UI             (port 5009 — notification_ui/server.py)
#
# Optional Celery worker + beat (for scheduled queries + anomaly subs):
#   set CELERY_ENABLED=1 to also start the worker + beat in the same pod.
#   Celery Beat owns:
#     - existing notification scheduler ticks
#     - new AI scheduled-query + anomaly evaluator ticks (registered in
#       app.notification_layer.celery_app.beat_schedule)
#   The asyncio fallback scheduler keeps notifications working when Celery
#   is disabled, but AI scheduled queries / anomaly subs require Celery.

set -u

STATUS_PORT="${STATUS_SERVICE_PORT:-8515}"
CHAT_PORT="${CHAT_SERVICE_PORT:-8517}"
AI_CHAT_PORT="${AI_CHAT_SERVICE_PORT:-8518}"
UI_PORT="${UI_PORT:-5009}"
CELERY_ENABLED="${CELERY_ENABLED:-0}"

echo "Starting Status + Notification API on port ${STATUS_PORT}..."
uvicorn app.main:app --host 0.0.0.0 --port "${STATUS_PORT}" &
API_PID=$!

echo "Starting Chat API on port ${CHAT_PORT}..."
uvicorn app.chat_main:app --host 0.0.0.0 --port "${CHAT_PORT}" &
CHAT_PID=$!

echo "Starting AI Chat API on port ${AI_CHAT_PORT}..."
uvicorn app.ai_chat_main:app --host 0.0.0.0 --port "${AI_CHAT_PORT}" &
AI_CHAT_PID=$!

echo "Starting Notification UI on port ${UI_PORT}..."
( cd /app/notification_ui && \
  uvicorn server:app --host 0.0.0.0 --port "${UI_PORT}" ) &
UI_PID=$!

WORKER_PID=""
BEAT_PID=""
if [ "${CELERY_ENABLED}" = "1" ]; then
  echo "Starting Celery worker..."
  celery -A app.notification_layer.celery_app worker \
         --loglevel=info -Q notification &
  WORKER_PID=$!

  echo "Starting Celery beat..."
  celery -A app.notification_layer.celery_app beat --loglevel=info &
  BEAT_PID=$!
fi

echo "All services started (API=${API_PID}, CHAT=${CHAT_PID}, AI_CHAT=${AI_CHAT_PID}, UI=${UI_PID}, WORKER=${WORKER_PID:-off}, BEAT=${BEAT_PID:-off})"

# Build the wait list dynamically — `wait -n` only honors PIDs that exist.
WAIT_PIDS=("${API_PID}" "${CHAT_PID}" "${AI_CHAT_PID}" "${UI_PID}")
if [ -n "${WORKER_PID}" ]; then WAIT_PIDS+=("${WORKER_PID}"); fi
if [ -n "${BEAT_PID}" ];   then WAIT_PIDS+=("${BEAT_PID}"); fi

# Wait for any to exit; if one dies, kill the rest and exit with its code.
wait -n "${WAIT_PIDS[@]}"
EXIT_CODE=$?

echo "A process exited with code ${EXIT_CODE}. Stopping all..."
kill "${WAIT_PIDS[@]}" 2>/dev/null
exit "${EXIT_CODE}"
