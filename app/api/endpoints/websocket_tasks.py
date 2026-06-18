"""
WebSocket endpoint for real-time task progress updates.

This module provides a WebSocket endpoint that connects to Redis
to fetch and stream Celery task progress updates to clients.
"""

from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from starlette.websockets import WebSocketState
from app.api.endpoints.dependencies.progress import get_progress
import asyncio
import logging

logger = logging.getLogger("app_logger")

router = APIRouter()


def _is_open(websocket: WebSocket) -> bool:
    """True iff the socket is still in a state that will accept a send.

    Starlette transitions `application_state` through CONNECTING →
    CONNECTED → DISCONNECTED. Calling `.send*` after the close frame
    has been emitted raises:
      RuntimeError: Cannot call "send" once a close message has been sent.
    Checking the state lets us drop the send instead of crashing the
    handler.
    """
    return websocket.application_state == WebSocketState.CONNECTED


async def _safe_send_json(websocket: WebSocket, payload: dict) -> bool:
    """Send if the socket is still open, swallow errors otherwise.

    Returns True on a successful send, False if the socket was already
    closed or the send raised (treated as client disconnect).
    """
    if not _is_open(websocket):
        return False
    try:
        await websocket.send_json(payload)
        return True
    except Exception as exc:
        # Most common: RuntimeError("Cannot call send once a close
        # message has been sent.") when the client disconnected between
        # the state check and the await. Log with exc_info so the type
        # surfaces in pod logs without crashing the loop.
        logger.info(
            "send_json dropped for task ws (likely client disconnect): %r",
            exc,
        )
        return False


async def _safe_close(websocket: WebSocket) -> None:
    """Close the socket idempotently — no-op if already closed."""
    if not _is_open(websocket):
        return
    try:
        await websocket.close()
    except Exception as exc:
        logger.debug("close raised on already-closing ws: %r", exc)


@router.websocket("/ws/tasks/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    """
    WebSocket endpoint to send task progress updates to the client.
    Polls the progress system every 2 seconds with proper error handling.
    Matches the API specification format.

    Args:
        websocket: WebSocket connection instance
        task_id: The Celery task ID to track
    """
    await websocket.accept()
    last_progress = -1
    last_status = None
    not_found_count = 0
    max_not_found = 30  # Close after 30 consecutive "not found" polls (~60 seconds)
    logger.info(f"WebSocket connection established for task: {task_id}")

    try:
        while True:
            # If the client disconnected during the last sleep, stop
            # polling immediately instead of trying to send into a
            # closed socket on the next iteration. This is the main
            # fix for the `Cannot call "send" once a close message has
            # been sent` errors we were seeing under client churn.
            if not _is_open(websocket):
                logger.info(
                    "Client disconnected for task %s; stopping poll loop.",
                    task_id,
                )
                break

            try:
                # Get progress data from Redis (custom progress key: task:{task_id})
                progress_data = get_progress(task_id)

                if not progress_data:
                    not_found_count += 1
                    if not_found_count >= max_not_found:
                        logger.info(f"Task {task_id} not found after {max_not_found} polls. Closing WebSocket.")
                        await _safe_send_json(websocket, {
                            "task_id": task_id,
                            "status": "NOT_FOUND",
                            "progress": 0,
                            "message": "Task not found. Connection closed."
                        })
                        await _safe_close(websocket)
                        break
                    await asyncio.sleep(2)
                    continue

                current_status = progress_data.get("status")
                # Custom progress uses "progress" field (0-100)
                current_progress = progress_data.get("progress", 0)

                # Track consecutive "not found" — if task returns PENDING with
                # "not found" message, it means the key doesn't exist in Redis
                if current_status == "PENDING" and "not found" in progress_data.get("message", "").lower():
                    not_found_count += 1
                    if not_found_count >= max_not_found:
                        logger.info(f"Task {task_id} not found in Redis after {max_not_found} polls. Closing WebSocket.")
                        await _safe_send_json(websocket, {
                            "task_id": task_id,
                            "status": "NOT_FOUND",
                            "progress": 0,
                            "message": "Task not found or already completed. Connection closed."
                        })
                        await _safe_close(websocket)
                        break
                    await asyncio.sleep(2)
                    continue
                else:
                    not_found_count = 0  # Reset counter when task is actually found

                # Send update if progress changed or status changed to terminal state
                # Terminal states: SUCCESS, FAILED, ERROR, CANCELLED
                terminal_states = ["SUCCESS", "FAILED", "ERROR", "CANCELLED"]
                should_send = (current_progress != last_progress or
                              (current_status in terminal_states and current_status != last_status))

                if should_send:
                    # Start with fixed fields (ensuring they are always present)
                    response_data = {
                        "task_id": progress_data.get("task_id", task_id),
                        "status": current_status,
                        "progress": current_progress,
                        "message": progress_data.get("message", "")
                    }

                    # Merge all other fields from Redis data (preserving fixed fields)
                    # This ensures all fields from Redis are included while keeping fixed fields
                    for key, value in progress_data.items():
                        if key not in response_data:
                            response_data[key] = value

                    sent = await _safe_send_json(websocket, response_data)
                    if not sent:
                        # Client gone — no point continuing to poll.
                        break
                    last_progress = current_progress
                    last_status = current_status

                    # Log progress updates
                    logger.debug(f"Sent progress update for task {task_id}: {current_status} - {current_progress}%")

                    # Close connection if task is completed, failed, or errored
                    if current_status in ["SUCCESS", "FAILED", "ERROR", "CANCELLED"]:
                        logger.info(f"Task {task_id} completed with status: {current_status}. Closing WebSocket connection.")
                        await _safe_close(websocket)
                        break

            except WebSocketDisconnect:
                # Client closed mid-iteration — stop cleanly without
                # trying to send the synthetic error packet below.
                logger.info(
                    "WebSocket disconnected during poll for task %s",
                    task_id,
                )
                break
            except Exception as e:
                # Real internal error (e.g. Redis unreachable, JSON
                # decode of corrupt progress blob). `exc_info=True`
                # captures the traceback in pod logs so we can
                # diagnose without having to reproduce. The synthetic
                # error packet goes out only if the client is still
                # listening — `_safe_send_json` no-ops otherwise so
                # we never re-raise here.
                logger.error(
                    "Error getting progress for task %s: %r",
                    task_id, e,
                    exc_info=True,
                )
                await _safe_send_json(websocket, {
                    "task_id": task_id,
                    "status": "ERROR",
                    "progress": 0,
                    "message": f"Failed to get progress: {e}",
                    "error": str(e),
                })
                break

            await asyncio.sleep(2)  # Poll every 2 seconds

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for task {task_id}")
    except Exception as e:
        # Anything that escapes the inner try is a real handler-level
        # failure (not a client churn artefact, which the inner except
        # now catches). Log with traceback for diagnosis.
        logger.error(
            "WebSocket error for task %s: %r",
            task_id, e,
            exc_info=True,
        )
    finally:
        await _safe_close(websocket)

