#!/bin/bash
# Start all processes: Status API, Notification UI, Celery Worker + Beat

echo "Starting Celery Worker..."
celery -A app.notification_layer.celery_app worker --loglevel=info --concurrency=2 -Q notification &
CELERY_PID=$!

echo "Starting Celery Beat..."
celery -A app.notification_layer.celery_app beat --loglevel=info &
BEAT_PID=$!

echo "Starting Status + Notification API on port 8515..."
uvicorn app.main:app --host 0.0.0.0 --port 8515 &
API_PID=$!

echo "Starting Notification UI on port 5009..."
cd /app/notification_ui
uvicorn server:app --host 0.0.0.0 --port 5009 &
UI_PID=$!
cd /app

echo "All processes started (API=$API_PID, UI=$UI_PID, Worker=$CELERY_PID, Beat=$BEAT_PID)"

# Wait for any to exit — if one dies, kill all and exit
wait -n $API_PID $UI_PID $CELERY_PID $BEAT_PID
EXIT_CODE=$?

echo "A process exited with code $EXIT_CODE. Stopping all..."
kill $API_PID $UI_PID $CELERY_PID $BEAT_PID 2>/dev/null
exit $EXIT_CODE
