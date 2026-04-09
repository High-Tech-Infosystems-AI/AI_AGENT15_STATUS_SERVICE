"""
Progress tracking module for fetching task progress from Redis.

This module connects to Redis to fetch task status and progress stored by report_progress.
Uses the custom key pattern: task:{task_id}
"""

import json
import logging
import redis
from typing import Dict, Optional
from app.core import settings

logger = logging.getLogger("app_logger")

# Redis connection for custom progress tracking
_redis_client: Optional[redis.Redis] = None
_notified_missing_tasks: set = set()  # Track tasks already logged as missing


def get_redis_client() -> redis.Redis:
    """
    Get or create Redis client connection for custom progress tracking.
    Uses REDIS_DB from settings (typically DB 0 for custom progress).
    
    Returns:
        redis.Redis: Redis client connected to the configured DB
    """
    global _redis_client
    
    if _redis_client is None:
        try:
            # Connect to Redis using the configured DB (typically 0 for custom progress)
            _redis_client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,  # Use configured DB (typically 0)
                password=settings.REDIS_PASSWORD if settings.REDIS_PASSWORD else None,
                decode_responses=True,  # Automatically decode responses to strings
                socket_connect_timeout=5,
                socket_timeout=5
            )
            # Test connection
            _redis_client.ping()
            logger.info(f"Connected to Redis (DB {settings.REDIS_DB}) at {settings.REDIS_HOST}:{settings.REDIS_PORT}")
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

