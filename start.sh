#!/bin/bash
# Start both the Status/Notification API (port 8515) and the Notification UI (port 5009)
# Celery Beat is optional — run separately if needed:
#   celery -A app.notification_layer.celery_app worker --loglevel=info -Q notification &
#   celery -A app.notification_layer.celery_app beat --loglevel=info &
# The asyncio fallback scheduler handles everything when Celery is not running.

echo "Starting Status + Notification API on port 8515..."
uvicorn app.main:app --host 0.0.0.0 --port 8515 &
API_PID=$!

echo "Starting Notification UI on port 5009..."
cd /app/notification_ui
uvicorn server:app --host 0.0.0.0 --port 5009 &
UI_PID=$!
cd /app

echo "Both services started (API PID=$API_PID, UI PID=$UI_PID)"

# Wait for either to exit — if one dies, kill the other and exit
wait -n $API_PID $UI_PID
EXIT_CODE=$?

echo "A process exited with code $EXIT_CODE. Stopping all..."
kill $API_PID $UI_PID 2>/dev/null
exit $EXIT_CODE
