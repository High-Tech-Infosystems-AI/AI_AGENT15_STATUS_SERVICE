"""Folder endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.dependencies.tenant_auth import AuthCtx, get_auth_ctx
from app.mail_layer import models as m, store
from app.mail_layer.schemas import FolderCreate, FolderOut, FolderUpdate

router = APIRouter()


@router.get("/accounts/{account_id}/folders", response_model=List[FolderOut])
def list_folders(account_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                 db: Session = Depends(get_db)):
    store.get_owned_account(db, ctx.user_id, account_id)
    rows = (db.query(m.MailFolder)
            .filter(m.MailFolder.account_id == account_id,
                    m.MailFolder.deleted_at.is_(None))
            .order_by(m.MailFolder.sort_order.asc(), m.MailFolder.name.asc()).all())
    return [store.serialize_folder(f) for f in rows]


@router.post("/accounts/{account_id}/folders", response_model=FolderOut)
def create_folder(account_id: int, body: FolderCreate,
                  ctx: AuthCtx = Depends(get_auth_ctx),
                  db: Session = Depends(get_db)):
    acct = store.get_owned_account(db, ctx.user_id, account_id)
    fold = m.MailFolder(
        account_id=acct.id, user_id=ctx.user_id,
        organization_id=ctx.organization_id, name=body.name, role="custom",
        parent_folder_id=body.parent_folder_id, is_system=0, sort_order=50)
    db.add(fold)
    db.commit()
    db.refresh(fold)
    return store.serialize_folder(fold)


@router.put("/folders/{folder_id}", response_model=FolderOut)
def update_folder(folder_id: int, body: FolderUpdate,
                  ctx: AuthCtx = Depends(get_auth_ctx),
                  db: Session = Depends(get_db)):
    fold = store.get_owned_folder(db, ctx.user_id, folder_id)
    if fold.is_system:
        raise HTTPException(400, "System folders cannot be renamed or moved")
    if body.name is not None:
        fold.name = body.name
    if body.parent_folder_id is not None:
        fold.parent_folder_id = body.parent_folder_id or None
    db.commit()
    db.refresh(fold)
    return store.serialize_folder(fold)


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                  db: Session = Depends(get_db)):
    fold = store.get_owned_folder(db, ctx.user_id, folder_id)
    if fold.is_system:
        raise HTTPException(400, "System folders cannot be deleted")
    trash = store.get_folder_by_role(db, fold.account_id, "trash")
    if trash:
        db.query(m.MailMessage).filter(
            m.MailMessage.folder_id == fold.id,
            m.MailMessage.deleted_at.is_(None)).update({"folder_id": trash.id})
        store.refresh_folder_counts(db, trash.id)
    fold.deleted_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/folders/{folder_id}/empty")
def empty_folder(folder_id: int, ctx: AuthCtx = Depends(get_auth_ctx),
                 db: Session = Depends(get_db)):
    """Empty a folder — used for Empty Junk / Empty Deleted Items."""
    fold = store.get_owned_folder(db, ctx.user_id, folder_id)
    now = datetime.utcnow()
    n = (db.query(m.MailMessage)
         .filter(m.MailMessage.folder_id == fold.id,
                 m.MailMessage.deleted_at.is_(None))
         .update({"deleted_at": now}))
    store.refresh_folder_counts(db, fold.id)
    db.commit()
    return {"ok": True, "removed": n}
