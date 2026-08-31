"""S3 storage for mail attachments. Reuses the chat attachments bucket
(``AWS_S3_BUCKET_CHAT``) under a ``mail/`` prefix; falls back cleanly when S3
isn't configured (attachment bytes are simply not persisted)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from app.core import settings

logger = logging.getLogger("app_logger")


def is_configured() -> bool:
    return bool(getattr(settings, "AWS_ACCESS_KEY_ID", "")
                and getattr(settings, "AWS_SECRET_ACCESS_KEY", "")
                and getattr(settings, "AWS_S3_BUCKET_CHAT", ""))


def _client():
    import boto3  # lazy — only the workers/attachment routes need it
    return boto3.client(
        "s3",
        aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", "") or None,
        aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", "") or None,
        region_name=getattr(settings, "AWS_REGION", "") or None,
        endpoint_url=getattr(settings, "AWS_S3_ENDPOINT_URL", "") or None,
    )


def upload(*, data: bytes, mime_type: str, file_name: str, user_id: int) -> Optional[str]:
    """Store bytes, return the s3_key (or None if S3 isn't configured)."""
    if not is_configured():
        return None
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
    yyyymm = datetime.utcnow().strftime("%Y-%m")
    s3_key = f"mail/{user_id}/{yyyymm}/{uuid.uuid4().hex}.{ext}"
    try:
        _client().put_object(
            Bucket=settings.AWS_S3_BUCKET_CHAT,
            Key=s3_key,
            Body=data,
            ContentType=mime_type or "application/octet-stream",
        )
        return s3_key
    except Exception as e:
        logger.error("mail S3 upload failed: %s", e)
        return None


def download(s3_key: str) -> Optional[bytes]:
    if not s3_key or not is_configured():
        return None
    try:
        obj = _client().get_object(Bucket=settings.AWS_S3_BUCKET_CHAT, Key=s3_key)
        return obj["Body"].read()
    except Exception as e:
        logger.error("mail S3 download failed for %s: %s", s3_key, e)
        return None


def presign(s3_key: Optional[str]) -> Optional[str]:
    if not s3_key or not is_configured():
        return None
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.AWS_S3_BUCKET_CHAT, "Key": s3_key},
            ExpiresIn=int(getattr(settings, "AWS_S3_PRESIGNED_TTL_SECONDS", 3600)),
        )
    except Exception as e:
        logger.warning("mail presign failed for %s: %s", s3_key, e)
        return None
