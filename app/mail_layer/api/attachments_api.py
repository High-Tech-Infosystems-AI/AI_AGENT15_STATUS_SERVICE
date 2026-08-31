"""Attachment upload / download endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.mail_layer import models as m, s3_mail_service, store
from app.mail_layer.schemas import AttachmentOut

router = APIRouter()

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _owned_attachment(db: Session, user_id: int, att_id: int) -> m.MailAttachment:
    att = (db.query(m.MailAttachment)
           .filter(m.MailAttachment.id == att_id,
                   m.MailAttachment.user_id == user_id).first())
    if not att:
        raise HTTPException(404, "Attachment not found")
    return att


@router.post("/attachments", response_model=AttachmentOut)
async def upload_attachment(draft_id: int = Query(...),
                            file: UploadFile = File(...),
                            ctx: AuthCtx = Depends(get_auth_ctx),
                            db: Session = Depends(get_db)):
    draft = store.get_owned_message(db, ctx.user_id, draft_id)
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "Attachment exceeds 25 MB limit")
    s3_key = s3_mail_service.upload(
        data=data, mime_type=file.content_type or "application/octet-stream",
        file_name=file.filename or "attachment", user_id=ctx.user_id)
    att = m.MailAttachment(
        message_id=draft.id, account_id=draft.account_id, user_id=ctx.user_id,
        s3_key=s3_key, file_name=(file.filename or "attachment")[:255],
        mime_type=(file.content_type or "application/octet-stream")[:120],
        size_bytes=len(data), is_inline=0, fetched=1 if s3_key else 0)
    db.add(att)
    draft.has_attachments = 1
    db.commit()
    db.refresh(att)
    return store.serialize_attachment(att)


@router.get("/attachments/{att_id}")
def download_attachment(att_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                        db: Session = Depends(get_db)):
    att = _owned_attachment(db, ctx.user_id, att_id)
    if not att.s3_key:
        raise HTTPException(404, "Attachment bytes are not stored")
    data = s3_mail_service.download(att.s3_key)
    if data is None:
        raise HTTPException(502, "Could not fetch the attachment")
    return Response(
        content=data, media_type=att.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{att.file_name}"'})


@router.get("/attachments/{att_id}/url")
def attachment_url(att_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                   db: Session = Depends(get_db)):
    att = _owned_attachment(db, ctx.user_id, att_id)
    url = s3_mail_service.presign(att.s3_key)
    if not url:
        raise HTTPException(404, "No downloadable URL")
    return {"url": url}


@router.delete("/attachments/{att_id}")
def delete_attachment(att_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                      db: Session = Depends(get_db)):
    att = _owned_attachment(db, ctx.user_id, att_id)
    msg_id = att.message_id
    db.delete(att)
    db.flush()
    remaining = (db.query(m.MailAttachment)
                 .filter(m.MailAttachment.message_id == msg_id).count())
    if remaining == 0:
        db.query(m.MailMessage).filter(m.MailMessage.id == msg_id).update(
            {"has_attachments": 0})
    db.commit()
    return {"ok": True}
