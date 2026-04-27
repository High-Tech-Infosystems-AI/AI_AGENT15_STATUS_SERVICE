"""Auth dependency for chat REST endpoints. Mirrors the existing
notification WS validation but for HTTP `Authorization: Bearer ...`."""
import hashlib
import json
import logging
from typing import Optional

import httpx
from fastapi import Header, HTTPException

from app.core import settings
from app.notification_layer import redis_manager

logger = logging.getLogger("app_logger")


def _validate_token(token: str) -> Optional[dict]:
    cache_key = "auth:token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    try:
        cached = redis_manager.get_notification_redis().get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        resp = httpx.post(settings.AUTH_SERVICE_URL,
                          params={"token": token},
                          headers={"accept": "application/json"},
                          timeout=5)
        if resp.status_code != 200:
            return None
        info = resp.json()
        if not info.get("user_id"):
            return None
        try:
            redis_manager.get_notification_redis().setex(
                cache_key, 60, json.dumps({
                    "user_id": info["user_id"],
                    "role_id": info.get("role_id"),
                    "role_name": info.get("role_name"),
                    "username": info.get("username"),
                    "name": info.get("name"),
                }))
        except Exception:
            pass
        return info
    except Exception as e:
        logger.error("token validation error: %s", e)
        return None


def current_user(authorization: str = Header(default="")) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    info = _validate_token(token)
    if not info:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return info
