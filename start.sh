#!/bin/bash
# Start the Status Service processes in this container:
#   1. Status / Notification API   (port 8515 — app.main:app)   [CRITICAL]
#   2. Chat API                    (port 8517 — app.chat_main:app)
#   3. AI Chat API                 (port 8518 — app.ai_chat_main:app)
#   4. Notification UI             (port 5009 — notification_ui/server.py)
#
# Optional Celery worker + beat (set CELERY_ENABLED=1).
#
# IMPORTANT: The Status API (8515) is the health-checked process that owns this
# service's Consul registration. Previously ANY subprocess crash (e.g. a chat
# segfault) tripped `wait -n`, which killed the whole container — so the pod
# crash-looped and the Status Service dropped out of Consul ("not registered").
#
# Now each NON-critical subprocess is supervised (auto-restart with backoff,
# then abandoned after a cap) and container liveness is tied ONLY to the Status
# API. A crash in chat / ai-chat / ui can no longer take the pod down or
# deregister the Status Service.

set -u

STATUS_PORT="${STATUS_SERVICE_PORT:-8515}"
CHAT_PORT="${CHAT_SERVICE_PORT:-8517}"
AI_CHAT_PORT="${AI_CHAT_SERVICE_PORT:-8518}"
UI_PORT="${UI_PORT:-5009}"
CELERY_ENABLED="${CELERY_ENABLED:-0}"

# Toggle a known-broken non-critical subprocess off entirely, if ever needed.
CHAT_ENABLED="${CHAT_ENABLED:-1}"
AI_CHAT_ENABLED="${AI_CHAT_ENABLED:-1}"
UI_ENABLED="${UI_ENABLED:-1}"

# supervise <name> <cmd...> : keep a non-critical process alive WITHOUT ever
# taking the pod down. Restarts with exponential backoff (cap 30s) and gives up
# after SUPERVISE_MAX_RETRIES failures so a hard-broken process (e.g. a
# reproducible segfault) can't hot-loop forever.
supervise() {
  local name="$1"; shift
  local max="${SUPERVISE_MAX_RETRIES:-6}"
  local n=0
  local delay=3
  while true; do
    echo "[supervisor] starting ${name} (attempt $((n + 1)))..."
    "$@"
    local code=$?
    n=$((n + 1))
    if [ "${code}" -eq 0 ]; then
      echo "[supervisor] ${name} exited cleanly (code 0); not restarting."
      break
    fi
    if [ "${n}" -ge "${max}" ]; then
      echo "[supervisor] ${name} failed ${n}x (last code ${code}); giving up. Pod stays up via the Status API."
      break
    fi
    echo "[supervisor] ${name} crashed (code ${code}); restart ${n}/${max} in ${delay}s"
    sleep "${delay}"
    if [ "${delay}" -lt 30 ]; then delay=$((delay * 2)); fi
  done
}

echo "Starting Status + Notification API on port ${STATUS_PORT}... [critical]"
uvicorn app.main:app --host 0.0.0.0 --port "${STATUS_PORT}" &
API_PID=$!

if [ "${CHAT_ENABLED}" = "1" ]; then
  echo "Starting (supervised) Chat API on port ${CHAT_PORT}..."
  supervise "chat" uvicorn app.chat_main:app --host 0.0.0.0 --port "${CHAT_PORT}" &
fi

if [ "${AI_CHAT_ENABLED}" = "1" ]; then
  echo "Starting (supervised) AI Chat API on port ${AI_CHAT_PORT}..."
  supervise "ai_chat" uvicorn app.ai_chat_main:app --host 0.0.0.0 --port "${AI_CHAT_PORT}" &
fi

if [ "${UI_ENABLED}" = "1" ]; then
  echo "Starting (supervised) Notification UI on port ${UI_PORT}..."
  ( cd /app/notification_ui && supervise "notification_ui" uvicorn server:app --host 0.0.0.0 --port "${UI_PORT}" ) &
fi

if [ "${CELERY_ENABLED}" = "1" ]; then
  echo "Starting (supervised) Celery worker + beat..."
  supervise "celery-worker" celery -A app.notification_layer.celery_app worker --loglevel=info -Q notification &
  supervise "celery-beat" celery -A app.notification_layer.celery_app beat --loglevel=info &
fi

echo "Status Service started (API=${API_PID}). Pod liveness is tied to the Status API only."

# Container liveness == the Status API (owns Consul registration + the 8515
# health check). If it dies, exit so k8s restarts the pod. Non-critical
# subprocess crashes are absorbed by supervise() and never reach here.
wait "${API_PID}"
EXIT_CODE=$?
echo "Status API (critical) exited with code ${EXIT_CODE}. Stopping pod."
exit "${EXIT_CODE}"
