"""
Redis Pub/Sub Manager for Notification Service.

Handles:
- Publishing notifications to per-user, broadcast, and banner channels
- Caching unread counts and active banners
- Distributed scheduler lock
"""

import json
import logging
from typing import Optional, List

import redis

from app.core import settings

logger = logging.getLogger("app_logger")

_redis_client: Optional[redis.Redis] = None
_pubsub_client: Optional[redis.Redis] = None


def get_notification_redis() -> redis.Redis:
    """Get or create the Redis client for notification operations."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD if settings.REDIS_PASSWORD else None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        _redis_client.ping()
        logger.info("Notification Redis client connected")
    return _redis_client


def get_pubsub_redis() -> redis.Redis:
    """Separate Redis connection for Pub/Sub (blocking subscriber needs its own conn)."""
    global _pubsub_client
    if _pubsub_client is None:
        _pubsub_client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD if settings.REDIS_PASSWORD else None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        _pubsub_client.ping()
        logger.info("Notification Pub/Sub Redis client connected")
    return _pubsub_client


# ---------------------------------------------------------------------------
# Pub/Sub Publishing
# ---------------------------------------------------------------------------

def publish_to_user(user_id: int, payload: dict) -> None:
    """Publish a notification to a specific user's channel."""
    try:
        client = get_notification_redis()
        channel = f"notif:user:{user_id}"
        client.publish(channel, json.dumps(payload, default=str))
    except Exception as e:
        logger.error("Failed to publish to user %s: %s", user_id, e)


def publish_to_users(user_ids: List[int], payload: dict) -> None:
    """Publish a notification to multiple user channels."""
    try:
        client = get_notification_redis()
        message = json.dumps(payload, default=str)
        pipe = client.pipeline(transaction=False)
        for uid in user_ids:
            pipe.publish(f"notif:user:{uid}", message)
        pipe.execute()
    except Exception as e:
        logger.error("Failed to publish to users: %s", e)


def publish_broadcast(payload: dict) -> None:
    """Publish to the broadcast channel (all users)."""
    try:
        client = get_notification_redis()
        client.publish("notif:broadcast", json.dumps(payload, default=str))
    except Exception as e:
        logger.error("Failed to publish broadcast: %s", e)


def publish_banner(action: str, payload: dict) -> None:
    """Publish banner event (create / expire)."""
    try:
        client = get_notification_redis()
        message = {"type": "banner", "action": action, "data": payload}
        client.publish("notif:banner", json.dumps(message, default=str))
    except Exception as e:
        logger.error("Failed to publish banner: %s", e)


# ---------------------------------------------------------------------------
# Unread Count Cache
# ---------------------------------------------------------------------------

def get_cached_unread_count(user_id: int) -> Optional[int]:
    try:
        client = get_notification_redis()
        val = client.get(f"notif:unread:{user_id}")
        return int(val) if val is not None else None
    except Exception as e:
        logger.error("Failed to get cached unread count for user %s: %s", user_id, e)
        return None


def set_cached_unread_count(user_id: int, count: int, ttl: int = 300) -> None:
    try:
        client = get_notification_redis()
        client.setex(f"notif:unread:{user_id}", ttl, count)
    except Exception as e:
        logger.error("Failed to cache unread count for user %s: %s", user_id, e)


def invalidate_unread_count(user_ids: List[int]) -> None:
    """Delete cached unread counts so next request re-fetches from DB."""
    try:
        client = get_notification_redis()
        if user_ids:
            keys = [f"notif:unread:{uid}" for uid in user_ids]
            client.delete(*keys)
    except Exception as e:
        logger.error("Failed to invalidate unread counts: %s", e)


# ---------------------------------------------------------------------------
# Banner Cache
# ---------------------------------------------------------------------------

def get_cached_banners() -> Optional[list]:
    try:
        client = get_notification_redis()
        val = client.get("notif:banners:active")
        return json.loads(val) if val else None
    except Exception as e:
        logger.error("Failed to get cached banners: %s", e)
        return None


def set_cached_banners(banners: list, ttl: int = 60) -> None:
    try:
        client = get_notification_redis()
        client.setex("notif:banners:active", ttl, json.dumps(banners, default=str))
    except Exception as e:
        logger.error("Failed to cache banners: %s", e)


def invalidate_banner_cache() -> None:
    try:
        client = get_notification_redis()
        client.delete("notif:banners:active")
    except Exception as e:
        logger.error("Failed to invalidate banner cache: %s", e)


# ---------------------------------------------------------------------------
# Scheduler Lock
# ---------------------------------------------------------------------------

def acquire_scheduler_lock(lock_name: str = "notif:schedule:lock", ttl: int = 30) -> bool:
    """Try to acquire a distributed lock. Returns True if acquired."""
    try:
        client = get_notification_redis()
        return bool(client.set(lock_name, "1", nx=True, ex=ttl))
    except Exception as e:
        logger.error("Failed to acquire scheduler lock: %s", e)
        return False


def release_scheduler_lock(lock_name: str = "notif:schedule:lock") -> None:
    try:
        client = get_notification_redis()
        client.delete(lock_name)
    except Exception as e:
        logger.error("Failed to release scheduler lock: %s", e)
