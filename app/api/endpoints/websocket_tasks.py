"""
WebSocket endpoint for real-time task progress updates.

This module provides a WebSocket endpoint that connects to Redis
to fetch and stream Celery task progress updates to clients.
"""

from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from app.api.endpoints.dependencies.progress import get_progress
import asyncio
import logging

logger = logging.getLogger("app_logger")

router = APIRouter()


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
            try:
                # Get progress data from Redis (custom progress key: task:{task_id})
                progress_data = get_progress(task_id)

                if not progress_data:
                    not_found_count += 1
                    if not_found_count >= max_not_found:
                        logger.info(f"Task {task_id} not found after {max_not_found} polls. Closing WebSocket.")
                        await websocket.send_json({
                            "task_id": task_id,
                            "status": "NOT_FOUND",
                            "progress": 0,
                            "message": "Task not found. Connection closed."
                        })
                        await websocket.close()
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
                        await websocket.send_json({
                            "task_id": task_id,
                            "status": "NOT_FOUND",
                            "progress": 0,
                            "message": "Task not found or already completed. Connection closed."
                        })
                        await websocket.close()
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

                    await websocket.send_json(response_data)
                    last_progress = current_progress
                    last_status = current_status

                    # Log progress updates
                    logger.debug(f"Sent progress update for task {task_id}: {current_status} - {current_progress}%")

                    # Close connection if task is completed, failed, or errored
                    if current_status in ["SUCCESS", "FAILED", "ERROR", "CANCELLED"]:
                        logger.info(f"Task {task_id} completed with status: {current_status}. Closing WebSocket connection.")
                        await websocket.close()
                        break
                        
            except Exception as e:
                logger.error(f"Error getting progress for task {task_id}: {str(e)}")
                # Send error message to client
                await websocket.send_json({
                    "task_id": task_id,
                    "status": "ERROR",
                    "progress": 0,
                    "message": f"Failed to get progress: {str(e)}",
                    "error": str(e)
                })
                break
                
            await asyncio.sleep(2)  # Poll every 2 seconds

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for task {task_id}")
    except Exception as e:
        logger.error(f"WebSocket error for task {task_id}: {str(e)}")
        try:
            await websocket.close()
        except:
            pass

