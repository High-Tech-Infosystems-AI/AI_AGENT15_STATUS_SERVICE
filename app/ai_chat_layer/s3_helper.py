"""S3 upload helper for AI-generated artifacts (charts, PDFs).

Reuses the chat S3 bucket + boto3 client wired in `chat_layer.s3_chat_service`
but writes under the `ai/{user_id}/{YYYY-MM}/{uuid}.{ext}` prefix so
artifacts are easy to find and lifecycle-rule independently.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from app.chat_layer.s3_chat_service import _client, _is_configured, presign_get
from app.core import settings

logger = logging.getLogger("app_logger")


def upload_ai_artifact(*, data: bytes, mime: str, user_id: int,
                       kind: str, ext: str) -> Tuple[str, Optional[str]]:
    """Persist `data` to S3 and return (s3_key, presigned_url).

    `kind` is a short tag used purely for logging / object metadata
    (e.g. "chart", "report"). `ext` is the file extension without dot.
    """
    if not _is_configured():
        raise RuntimeError("S3 not configured for AI artifacts")
    yyyymm = datetime.utcnow().strftime("%Y-%m")
    key = f"ai/{user_id}/{yyyymm}/{uuid.uuid4().hex}.{ext}"
    try:
        _client().put_object(
            Bucket=settings.AWS_S3_BUCKET_CHAT,
            Key=key,
            Body=data,
            ContentType=mime,
            Metadata={"kind": kind, "user_id": str(user_id)},
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("AI artifact upload failed: %s", exc)
        raise
    return key, presign_get(key)
