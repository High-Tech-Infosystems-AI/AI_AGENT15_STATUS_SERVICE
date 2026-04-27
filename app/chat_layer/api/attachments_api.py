"""Chat attachment upload + URL endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from app.chat_layer import s3_chat_service as s3
from app.chat_layer.auth import current_user
from app.chat_layer.models import ChatMessageAttachment
from app.chat_layer.schemas import AttachmentOut, ErrorResponse
from app.database_Layer.db_config import SessionLocal

router = APIRouter()


def _err(code, msg, status):
    return JSONResponse(status_code=status, content={"error_code": code, "message": msg})


def _persist_attachment(db, *, s3_key, mime_type, file_name, size_bytes,
                        duration_seconds, uploaded_by) -> ChatMessageAttachment:
    row = ChatMessageAttachment(
        s3_key=s3_key, mime_type=mime_type, file_name=file_name,
        size_bytes=size_bytes, duration_seconds=duration_seconds,
        uploaded_by=uploaded_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/attachments", response_model=AttachmentOut,
             responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}})
async def upload_attachment(
    conversation_id: int = Form(...),
    duration_seconds: Optional[int] = Form(default=None),
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    data = await file.read()
    mime = file.content_type or "application/octet-stream"
    try:
        meta = s3.upload_attachment(
            data=data, mime_type=mime, file_name=file.filename or "upload.bin",
            uploaded_by=user["user_id"], conversation_id=conversation_id,
            duration_seconds=duration_seconds,
        )
    except ValueError as ve:
        msg = str(ve).lower()
        if "size" in msg or "too large" in msg:
            return _err("CHAT_ATTACHMENT_TOO_LARGE", str(ve), 413)
        return _err("CHAT_ATTACHMENT_TYPE_NOT_ALLOWED", str(ve), 415)
    db = SessionLocal()
    try:
        row = _persist_attachment(
            db, s3_key=meta["s3_key"], mime_type=meta["mime_type"],
            file_name=meta["file_name"], size_bytes=meta["size_bytes"],
            duration_seconds=meta.get("duration_seconds"),
            uploaded_by=user["user_id"],
        )
        return AttachmentOut(
            id=row.id, mime_type=row.mime_type, file_name=row.file_name,
            size_bytes=row.size_bytes, duration_seconds=row.duration_seconds,
            url=s3.presign_get(row.s3_key),
        ).model_dump(mode="json")
    finally:
        db.close()


@router.get("/attachments/{attachment_id}/url", response_model=AttachmentOut,
            responses={404: {"model": ErrorResponse}})
def get_attachment_url(attachment_id: int, user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        row = db.get(ChatMessageAttachment, attachment_id)
        if not row:
            return _err("CHAT_NOT_FOUND", "Attachment not found", 404)
        return AttachmentOut(
            id=row.id, mime_type=row.mime_type, file_name=row.file_name,
            size_bytes=row.size_bytes, duration_seconds=row.duration_seconds,
            url=s3.presign_get(row.s3_key),
        ).model_dump(mode="json")
    finally:
        db.close()
