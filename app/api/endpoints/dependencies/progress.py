"""
Progress tracking module for fetching task progress from Redis.

This module connects to Redis to fetch task status and progress stored by report_progress.
Uses the custom key pattern: task:{task_id}
"""

import json
import logging
import os
import redis
from typing import Dict, Optional
from app.core import settings

logger = logging.getLogger("app_logger")

# Redis connection for custom progress tracking
_redis_client: Optional[redis.Redis] = None
_notified_missing_tasks: set = set()  # Track tasks already logged as missing

# Per-env progress DB. MUST match the resume analyzer's PROGRESS_REDIS_DB
# (and any other writer's) so reads and writes hit the same keyspace.
# Override with PROGRESS_REDIS_DB env var if defaults don't fit your setup.
_ENV = os.getenv("ENV", "dev").lower().strip()
_DEFAULT_PROGRESS_DB = {"prod": 0, "stage": 1, "dev": 8}.get(_ENV, 9)
PROGRESS_REDIS_DB = int(os.getenv("PROGRESS_REDIS_DB", _DEFAULT_PROGRESS_DB))


def get_redis_client() -> redis.Redis:
    """
    Get or create Redis client connection for custom progress tracking.

    Returns:
        redis.Redis: Redis client connected to the per-env progress DB
    """
    global _redis_client

    if _redis_client is None:
        try:
            _redis_client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=PROGRESS_REDIS_DB,
                password=settings.REDIS_PASSWORD if settings.REDIS_PASSWORD else None,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            _redis_client.ping()
            logger.info(
                f"Connected to progress Redis (DB {PROGRESS_REDIS_DB}, ENV={_ENV}) "
                f"at {settings.REDIS_HOST}:{settings.REDIS_PORT}"
            )
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {str(e)}")
            raise

    return _redis_client


def get_progress(task_id: str) -> Optional[Dict]:
    """
    Fetch task progress from Redis using custom progress key pattern.
    
    The progress is stored in Redis with the key pattern:
    - task:{task_id}
    
    The data structure matches report_progress format:
    - task_id: Task identifier
    - status: Task status (QUEUE, IN_PROGRESS, SUCCESS, FAILED, etc.)
    - progress: Progress percentage (0-100)
    - message: Status message
    - type: Task type (default: "jd")
    - error: Error message if any
    - updated_at: Timestamp
    
    Args:
        task_id: The task ID
        
    Returns:
        Dict containing:
            - task_id: Task identifier
            - status: Task status
            - progress: Progress percentage (0-100)
            - message: Status message
            - type: Task type (if available)
            - error: Error message (if status is FAILED)
            - updated_at: Timestamp (if available)
        None if task not found or error occurred
    """
    if not task_id or not isinstance(task_id, str):
        logger.error("Invalid task_id provided to get_progress")
        return None
    
    try:
        redis_client = get_redis_client()
        
        # Custom progress uses this key pattern (matches report_progress)
        task_key = f"task:{task_id}"
        
        # Get task progress from Redis
        task_data = redis_client.get(task_key)
        
        if not task_data:
            if task_id not in _notified_missing_tasks:
                logger.debug(f"No progress data found for task {task_id}")
                _notified_missing_tasks.add(task_id)
                # Prevent unbounded growth — cap at 1000 entries
                if len(_notified_missing_tasks) > 1000:
                    _notified_missing_tasks.clear()
            return {
                "task_id": task_id,
                "status": "PENDING",
                "progress": 0,
                "message": "Task not found or not started yet"
            }
        
        # Parse the JSON data
        try:
            progress_data = json.loads(task_data)
            logger.debug(f"Retrieved progress for task {task_id}: {progress_data.get('status')} - {progress_data.get('progress')}%")
            return progress_data
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for task {task_id}: {e}")
            return {
                "task_id": task_id,
                "status": "ERROR",
                "progress": 0,
                "message": f"Failed to parse task data: {str(e)}"
            }
        
    except redis.ConnectionError as e:
        logger.error(f"Redis connection error while fetching progress for {task_id}: {str(e)}")
        return {
            "task_id": task_id,
            "status": "ERROR",
            "progress": 0,
            "message": f"Redis connection error: {str(e)}"
        }
    except redis.RedisError as e:
        logger.error(f"Redis error retrieving progress for task {task_id}: {e}")
        return {
            "task_id": task_id,
            "status": "ERROR",
            "progress": 0,
            "message": f"Redis error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving progress for task {task_id}: {e}", exc_info=True)
        return {
            "task_id": task_id,
            "status": "ERROR",
            "progress": 0,
            "message": f"Error fetching progress: {str(e)}"
        }

