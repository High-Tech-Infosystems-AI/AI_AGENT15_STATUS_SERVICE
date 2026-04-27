"""S3 service for chat attachments. Modeled on AI_AGENT11_RBAC_Service/app/utils_layer/s3_service.py."""
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core import settings

logger = logging.getLogger("app_logger")

ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
ALLOWED_VOICE_MIMES = {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg"}
ALLOWED_FILE_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/zip",
    "text/plain",
} | ALLOWED_IMAGE_MIMES

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VOICE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024


def _category_for(mime: str) -> str:
    if mime in ALLOWED_IMAGE_MIMES:
        return "image"
    if mime in ALLOWED_VOICE_MIMES:
        return "voice"
    if mime in ALLOWED_FILE_MIMES:
        return "file"
    raise ValueError(f"MIME type not allowed: {mime}")


def _max_bytes_for(category: str) -> int:
    return {"image": MAX_IMAGE_BYTES, "voice": MAX_VOICE_BYTES, "file": MAX_FILE_BYTES}[category]


def _is_configured() -> bool:
    return bool(getattr(settings, "AWS_ACCESS_KEY_ID", "") and
                getattr(settings, "AWS_SECRET_ACCESS_KEY", "") and
                getattr(settings, "AWS_S3_BUCKET_CHAT", ""))


def _client():
    return boto3.client(
        "s3",
        aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", "") or None,
        aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", "") or None,
        region_name=getattr(settings, "AWS_REGION", "") or None,
        endpoint_url=getattr(settings, "AWS_S3_ENDPOINT_URL", "") or None,
    )


def upload_attachment(*, data: bytes, mime_type: str, file_name: str,
                      uploaded_by: int, conversation_id: int,
                      duration_seconds: Optional[int] = None) -> dict:
    category = _category_for(mime_type)
    if len(data) > _max_bytes_for(category):
        raise ValueError(f"Attachment size {len(data)} exceeds limit for {category}")
    if not _is_configured():
        raise RuntimeError("S3 not configured")
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
    yyyymm = datetime.utcnow().strftime("%Y-%m")
    s3_key = f"chat/{conversation_id}/{yyyymm}/{uuid.uuid4().hex}.{ext}"
    try:
        _client().put_object(
            Bucket=settings.AWS_S3_BUCKET_CHAT,
            Key=s3_key,
            Body=data,
            ContentType=mime_type,
        )
    except (BotoCoreError, ClientError) as e:
        logger.error("chat S3 upload failed: %s", e)
        raise
    return {
        "s3_key": s3_key,
        "mime_type": mime_type,
        "file_name": file_name,
        "size_bytes": len(data),
        "duration_seconds": duration_seconds,
        "category": category,
        "uploaded_by": uploaded_by,
    }


_URL_MEMO: dict = {}
_URL_MEMO_LOCK = threading.Lock()
_URL_MEMO_MAX = 4096


def presign_get(s3_key: Optional[str]) -> Optional[str]:
    if not s3_key or not _is_configured():
        return None
    now = time.time()
    with _URL_MEMO_LOCK:
        entry = _URL_MEMO.get(s3_key)
        if entry and entry[1] > now:
            return entry[0]
    try:
        url = _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.AWS_S3_BUCKET_CHAT, "Key": s3_key},
            ExpiresIn=int(getattr(settings, "AWS_S3_PRESIGNED_TTL_SECONDS", 3600)),
        )
    except (BotoCoreError, ClientError) as e:
        logger.warning("chat presign failed for %s: %s", s3_key, e)
        return None
    with _URL_MEMO_LOCK:
        if len(_URL_MEMO) >= _URL_MEMO_MAX:
            _URL_MEMO.clear()
        ttl = int(getattr(settings, "AWS_S3_PRESIGNED_TTL_SECONDS", 3600)) // 2
        _URL_MEMO[s3_key] = (url, now + ttl)
    return url
