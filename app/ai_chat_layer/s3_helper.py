"""S3 upload / download helpers + the artifact registry.

Reuses the chat S3 bucket + boto3 client wired in `chat_layer.s3_chat_service`
but writes under the `ai/{user_id}/{YYYY-MM}/{uuid}.{ext}` prefix so
artifacts are easy to find and lifecycle-rule independently.

Every successful upload is paired with a row in the `ai_artifact` table
(via `register_ai_artifact`) so users can list their prior reports /
charts / CSVs / markdown exports and re-issue signed URLs after the
original ones expire.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.chat_layer.s3_chat_service import _client, _is_configured, presign_get
from app.core import settings

logger = logging.getLogger("app_logger")


def upload_ai_artifact(*, data: bytes, mime: str, user_id: int,
                       kind: str, ext: str) -> Tuple[str, Optional[str]]:
    """Persist `data` to S3 and return (s3_key, presigned_url).

    `kind` is a short tag used purely for logging / object metadata
    (e.g. "chart", "report", "csv", "markdown"). `ext` is the file
    extension without dot.
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


def download_ai_artifact(s3_key: str) -> Optional[bytes]:
    """Fetch the raw bytes for an artifact. Returns None on miss."""
    if not _is_configured():
        return None
    try:
        resp = _client().get_object(
            Bucket=settings.AWS_S3_BUCKET_CHAT, Key=s3_key,
        )
        return resp["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        logger.warning("AI artifact download failed for %s: %s", s3_key, exc)
        return None


def register_ai_artifact(
    *, db: Optional[Session], user_id: int, kind: str, s3_key: str,
    mime: str, file_name: Optional[str] = None,
    title: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Insert an `ai_artifact` row and return its id. Failures are
    logged but never raised — registry is best-effort, the actual S3
    object is what matters."""
    if db is None:
        return None
    try:
        from app.ai_chat_layer.models import AiArtifact
    except Exception:
        return None
    try:
        row = AiArtifact(
            user_id=int(user_id),
            kind=str(kind)[:32],
            s3_key=str(s3_key)[:512],
            mime=str(mime)[:80],
            file_name=(str(file_name)[:255] if file_name else None),
            title=(str(title)[:200] if title else None),
            meta=meta or None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return int(row.id)
    except Exception as exc:
        logger.warning("ai_artifact insert failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None
