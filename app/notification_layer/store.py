"""
Notification DB Store – CRUD operations for notification tables.
"""

import json
import logging
import math
from datetime import datetime
from typing import Optional, List, Tuple

from sqlalchemy import and_, or_, func, case
from sqlalchemy.orm import Session

from app.notification_layer.models import (
    Notification, NotificationRecipient, NotificationSchedule, NotificationEvent,
)
from app.database_Layer.db_model import User, Role, JobOpenings

logger = logging.getLogger("app_logger")


class TargetValidationError(Exception):
    """Raised when target_type/target_id resolution fails validation.
    The endpoint catches this and returns HTTP 400.
    """
    def __init__(self, message: str, code: str = "INVALID_TARGET"):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Target Resolution
# ---------------------------------------------------------------------------

def resolve_target_user_ids(
    db: Session,
    target_type: str,
    target_id: Optional[str],
    include_admins: bool = False,
) -> List[int]:
    """
    Resolve target specification into a list of concrete user IDs.

    Args:
        target_type: all | user | job | role
        target_id: csv user_ids | job internal id | role name
        include_admins: always include admin + super_admin users

    Raises:
        TargetValidationError: when target_type/target_id is invalid,
        the referenced entity (job/user/role) doesn't exist, or the
        resolution returns no recipients.
    """
    from sqlalchemy import text

    # 1. Validate target_type
    valid_target_types = {"all", "user", "job", "role"}
    if target_type not in valid_target_types:
        raise TargetValidationError(
            f"Invalid target_type '{target_type}'. Must be one of: {sorted(valid_target_types)}",
            code="INVALID_TARGET_TYPE",
        )

    # 2. Validate target_id is required for non-'all' types
    if target_type != "all" and not target_id:
        raise TargetValidationError(
            f"target_id is required when target_type is '{target_type}'",
            code="MISSING_TARGET_ID",
        )

    user_ids: set = set()

    if target_type == "all":
        rows = db.query(User.id).filter(User.deleted_at.is_(None)).all()
        user_ids = {r[0] for r in rows}

    elif target_type == "user":
        # Parse + validate ID format
        requested_ids: set = set()
        for part in target_id.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise TargetValidationError(
                    f"Invalid user_id '{part}' in target_id. Must be a positive integer.",
                    code="INVALID_USER_ID_FORMAT",
                )
            requested_ids.add(int(part))

        if not requested_ids:
            raise TargetValidationError(
                "target_id must contain at least one valid user_id",
                code="EMPTY_USER_LIST",
            )

        # Verify all requested user IDs actually exist (and not soft-deleted)
        existing_rows = (
            db.query(User.id)
            .filter(User.id.in_(list(requested_ids)), User.deleted_at.is_(None))
            .all()
        )
        existing_ids = {r[0] for r in existing_rows}
        missing = requested_ids - existing_ids
        if missing:
            raise TargetValidationError(
                f"User(s) not found or deleted: {sorted(missing)}",
                code="USER_NOT_FOUND",
            )
        user_ids = existing_ids

    elif target_type == "job":
        # Parse + validate ID format
        try:
            job_int_id = int(target_id)
        except (ValueError, TypeError):
            raise TargetValidationError(
                f"Invalid job_id '{target_id}'. Must be a positive integer (internal job id).",
                code="INVALID_JOB_ID_FORMAT",
            )

        # Verify the job exists (and not soft-deleted)
        job = (
            db.query(JobOpenings.id)
            .filter(JobOpenings.id == job_int_id, JobOpenings.deleted_at.is_(None))
            .first()
        )
        if not job:
            raise TargetValidationError(
                f"Job with id={job_int_id} not found or deleted",
                code="JOB_NOT_FOUND",
            )

        # Get all users assigned to this job
        rows = db.execute(
            text("SELECT user_id FROM user_jobs_assigned WHERE job_id = :jid"),
            {"jid": job_int_id},
        ).fetchall()
        assigned_ids = {r[0] for r in rows}

        # Filter to only existing/active users (assigned table may have stale IDs)
        if assigned_ids:
            existing_rows = (
                db.query(User.id)
                .filter(User.id.in_(list(assigned_ids)), User.deleted_at.is_(None))
                .all()
            )
            user_ids = {r[0] for r in existing_rows}

        if not user_ids and not include_admins:
            raise TargetValidationError(
                f"Job {job_int_id} has no users assigned",
                code="JOB_NO_RECIPIENTS",
            )

    elif target_type == "role":
        role_names = [r.strip().lower() for r in target_id.split(",") if r.strip()]
        if not role_names:
            raise TargetValidationError(
                "target_id must contain at least one role name",
                code="EMPTY_ROLE_LIST",
            )

        # Verify all requested roles exist
        existing_role_rows = db.query(Role.name).filter(
            func.lower(Role.name).in_(role_names)
        ).all()
        existing_role_names = {r[0].lower() for r in existing_role_rows if r[0]}
        missing_roles = set(role_names) - existing_role_names
        if missing_roles:
            raise TargetValidationError(
                f"Role(s) not found: {sorted(missing_roles)}",
                code="ROLE_NOT_FOUND",
            )

        rows = (
            db.query(User.id)
            .join(Role, User.role_id == Role.id)
            .filter(
                func.lower(Role.name).in_(role_names),
                User.deleted_at.is_(None),
            )
            .all()
        )
        user_ids = {r[0] for r in rows}

        if not user_ids and not include_admins:
            raise TargetValidationError(
                f"No active users found with role(s): {role_names}",
                code="ROLE_NO_RECIPIENTS",
            )

    # Always include admin/super_admin for restricted notifications
    if include_admins:
        admin_rows = (
            db.query(User.id)
            .join(Role, User.role_id == Role.id)
            .filter(
                func.lower(Role.name).in_(["admin", "super_admin"]),
                User.deleted_at.is_(None),
            )
            .all()
        )
        user_ids.update(r[0] for r in admin_rows)

    # Final safety net: drop any IDs that somehow don't exist (defensive)
    if user_ids:
        existing_rows = (
            db.query(User.id)
            .filter(User.id.in_(list(user_ids)))
            .all()
        )
        existing_ids = {r[0] for r in existing_rows}
        invalid = user_ids - existing_ids
        if invalid:
            logger.warning("Dropped %d stale user_ids: %s", len(invalid), sorted(invalid))
        user_ids = existing_ids

    if not user_ids:
        raise TargetValidationError(
            f"Target '{target_type}' resolved to zero recipients",
            code="NO_RECIPIENTS",
        )

    return list(user_ids)


# ---------------------------------------------------------------------------
# Create Notification + Recipients
# ---------------------------------------------------------------------------

def create_notification(
    db: Session,
    title: str,
    message: str,
    delivery_mode: str,
    domain_type: str,
    visibility: str,
    priority: str,
    target_type: str,
    target_id: Optional[str],
    source_service: Optional[str] = None,
    event_type: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_by: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> Tuple[Notification, List[int]]:
    """
    Create a notification record and resolve + insert recipients.
    Returns (notification, list_of_recipient_user_ids).
    """
    # Resolve recipients FIRST — before any DB writes.
    # If validation fails (invalid job/user/role), TargetValidationError
    # is raised here and no notification row is created (clean rollback).
    include_admins = target_type != "all"  # 'all' already includes everyone
    user_ids = resolve_target_user_ids(db, target_type, target_id, include_admins=include_admins)

    # Now safe to create the notification record
    notif = Notification(
        title=title,
        message=message,
        delivery_mode=delivery_mode,
        domain_type=domain_type,
        visibility=visibility,
        priority=priority,
        target_type=target_type,
        target_id=target_id,
        source_service=source_service,
        event_type=event_type,
        extra_metadata=json.dumps(metadata) if metadata else None,
        created_by=created_by,
        expires_at=expires_at,
    )
    db.add(notif)
    db.flush()  # get notif.id

    # Batch insert recipients
    recipient_objects = [
        NotificationRecipient(notification_id=notif.id, user_id=uid)
        for uid in user_ids
    ]
    db.bulk_save_objects(recipient_objects)

    db.commit()
    db.refresh(notif)

    logger.info(
        "Created notification %s (%s/%s/%s) → %d recipients",
        notif.id, delivery_mode, domain_type, visibility, len(user_ids),
    )
    return notif, user_ids


# ---------------------------------------------------------------------------
# Get User Notifications (paginated + filtered)
# ---------------------------------------------------------------------------

def get_user_notifications(
    db: Session,
    user_id: int,
    page: int = 1,
    limit: int = 25,
    domain_type: Optional[str] = None,
    visibility: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    priority: Optional[str] = None,
    is_read: Optional[bool] = None,
    delivery_mode: Optional[str] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[list, int, int]:
    """
    Returns (notifications_with_read_status, total_count, unread_count).
    """
    query = (
        db.query(Notification, NotificationRecipient.is_read, NotificationRecipient.read_at)
        .join(NotificationRecipient, Notification.id == NotificationRecipient.notification_id)
        .filter(
            NotificationRecipient.user_id == user_id,
            Notification.is_active == 1,
        )
    )

    # Filters
    if domain_type:
        types = [t.strip() for t in domain_type.split(",")]
        query = query.filter(Notification.domain_type.in_(types))
    if visibility:
        vis = [v.strip() for v in visibility.split(",")]
        query = query.filter(Notification.visibility.in_(vis))
    if date_from:
        query = query.filter(Notification.created_at >= date_from)
    if date_to:
        query = query.filter(Notification.created_at <= date_to)
    if priority:
        pris = [p.strip() for p in priority.split(",")]
        query = query.filter(Notification.priority.in_(pris))
    if is_read is not None:
        query = query.filter(NotificationRecipient.is_read == (1 if is_read else 0))
    if delivery_mode:
        query = query.filter(Notification.delivery_mode == delivery_mode)

    total = query.count()

    # Unread count (for this user, unfiltered)
    unread = (
        db.query(func.count(NotificationRecipient.id))
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .filter(
            NotificationRecipient.user_id == user_id,
            NotificationRecipient.is_read == 0,
            Notification.is_active == 1,
        )
        .scalar()
    ) or 0

    # Sorting
    sort_col = getattr(Notification, sort_by, Notification.created_at)
    order = sort_col.desc() if sort_order == "desc" else sort_col.asc()
    query = query.order_by(order)

    # Pagination
    offset = (page - 1) * limit
    rows = query.offset(offset).limit(limit).all()

    results = []
    for notif, read_flag, read_at in rows:
        meta = None
        if notif.extra_metadata:
            try:
                meta = json.loads(notif.extra_metadata)
            except (json.JSONDecodeError, TypeError):
                meta = notif.extra_metadata
        results.append({
            "id": notif.id,
            "title": notif.title,
            "message": notif.message,
            "delivery_mode": notif.delivery_mode,
            "domain_type": notif.domain_type,
            "visibility": notif.visibility,
            "priority": notif.priority,
            "source_service": notif.source_service,
            "event_type": notif.event_type,
            "metadata": meta,
            "created_at": notif.created_at,
            "is_read": bool(read_flag),
            "read_at": read_at,
        })

    return results, total, unread


# ---------------------------------------------------------------------------
# Admin Logs (paginated + filtered) — sees EVERYTHING
# ---------------------------------------------------------------------------

def get_admin_notification_logs(
    db: Session,
    page: int = 1,
    limit: int = 25,
    domain_type: Optional[str] = None,
    visibility: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    priority: Optional[str] = None,
    source_service: Optional[str] = None,
    event_type: Optional[str] = None,
    user_id: Optional[int] = None,
    created_by: Optional[int] = None,
    delivery_mode: Optional[str] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[list, int]:
    """
    Admin view — returns all notifications with recipient stats.
    """
    query = db.query(Notification).filter(Notification.is_active == 1)

    if domain_type:
        types = [t.strip() for t in domain_type.split(",")]
        query = query.filter(Notification.domain_type.in_(types))
    if visibility:
        vis = [v.strip() for v in visibility.split(",")]
        query = query.filter(Notification.visibility.in_(vis))
    if date_from:
        query = query.filter(Notification.created_at >= date_from)
    if date_to:
        query = query.filter(Notification.created_at <= date_to)
    if priority:
        pris = [p.strip() for p in priority.split(",")]
        query = query.filter(Notification.priority.in_(pris))
    if source_service:
        svcs = [s.strip() for s in source_service.split(",")]
        query = query.filter(Notification.source_service.in_(svcs))
    if event_type:
        query = query.filter(Notification.event_type == event_type)
    if created_by is not None:
        query = query.filter(Notification.created_by == created_by)
    if delivery_mode:
        query = query.filter(Notification.delivery_mode == delivery_mode)

    # Filter by recipient user
    if user_id is not None:
        query = query.filter(
            Notification.id.in_(
                db.query(NotificationRecipient.notification_id)
                .filter(NotificationRecipient.user_id == user_id)
                .subquery()
            )
        )

    total = query.count()

    sort_col = getattr(Notification, sort_by, Notification.created_at)
    order = sort_col.desc() if sort_order == "desc" else sort_col.asc()
    query = query.order_by(order)

    offset = (page - 1) * limit
    notifications = query.offset(offset).limit(limit).all()

    results = []
    for notif in notifications:
        # Get recipient stats
        recipients_count = db.query(func.count(NotificationRecipient.id)).filter(
            NotificationRecipient.notification_id == notif.id
        ).scalar() or 0

        read_count = db.query(func.count(NotificationRecipient.id)).filter(
            NotificationRecipient.notification_id == notif.id,
            NotificationRecipient.is_read == 1,
        ).scalar() or 0

        # Creator name
        creator_name = None
        if notif.created_by:
            creator = db.query(User.name).filter(User.id == notif.created_by).scalar()
            creator_name = creator

        meta = None
        if notif.extra_metadata:
            try:
                meta = json.loads(notif.extra_metadata)
            except (json.JSONDecodeError, TypeError):
                meta = notif.extra_metadata

        results.append({
            "id": notif.id,
            "title": notif.title,
            "message": notif.message,
            "delivery_mode": notif.delivery_mode,
            "domain_type": notif.domain_type,
            "visibility": notif.visibility,
            "priority": notif.priority,
            "target_type": notif.target_type,
            "target_id": notif.target_id,
            "source_service": notif.source_service,
            "event_type": notif.event_type,
            "metadata": meta,
            "created_by": notif.created_by,
            "created_by_name": creator_name,
            "created_at": notif.created_at,
            "expires_at": notif.expires_at,
            "is_active": bool(notif.is_active),
            "recipients_count": recipients_count,
            "read_count": read_count,
        })

    return results, total


# ---------------------------------------------------------------------------
# Unread Count
# ---------------------------------------------------------------------------

def get_unread_count(db: Session, user_id: int) -> int:
    return (
        db.query(func.count(NotificationRecipient.id))
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .filter(
            NotificationRecipient.user_id == user_id,
            NotificationRecipient.is_read == 0,
            Notification.is_active == 1,
        )
        .scalar()
    ) or 0


# ---------------------------------------------------------------------------
# Mark Read
# ---------------------------------------------------------------------------

def mark_notification_read(db: Session, notification_id: int, user_id: int) -> bool:
    recipient = db.query(NotificationRecipient).filter(
        NotificationRecipient.notification_id == notification_id,
        NotificationRecipient.user_id == user_id,
    ).first()
    if not recipient:
        return False
    recipient.is_read = 1
    recipient.read_at = datetime.utcnow()
    db.commit()
    return True


def mark_all_read(db: Session, user_id: int) -> int:
    """Mark all unread notifications as read for a user. Returns count updated."""
    count = (
        db.query(NotificationRecipient)
        .filter(
            NotificationRecipient.user_id == user_id,
            NotificationRecipient.is_read == 0,
        )
        .update({"is_read": 1, "read_at": datetime.utcnow()})
    )
    db.commit()
    return count


# ---------------------------------------------------------------------------
# Active Banners
# ---------------------------------------------------------------------------

def get_active_banners(db: Session) -> list:
    banners = (
        db.query(Notification)
        .filter(
            Notification.delivery_mode == "banner",
            Notification.is_active == 1,
            or_(
                Notification.expires_at.is_(None),
                Notification.expires_at > datetime.utcnow(),
            ),
        )
        .order_by(Notification.created_at.desc())
        .all()
    )
    results = []
    for b in banners:
        meta = None
        if b.extra_metadata:
            try:
                meta = json.loads(b.extra_metadata)
            except (json.JSONDecodeError, TypeError):
                meta = b.extra_metadata
        results.append({
            "id": b.id,
            "title": b.title,
            "message": b.message,
            "priority": b.priority,
            "domain_type": b.domain_type,
            "expires_at": b.expires_at,
            "created_at": b.created_at,
            "metadata": meta,
        })
    return results


# ---------------------------------------------------------------------------
# Schedule CRUD
# ---------------------------------------------------------------------------

def create_schedule(db: Session, **kwargs) -> NotificationSchedule:
    sched = NotificationSchedule(**kwargs)
    if sched.extra_metadata and isinstance(sched.extra_metadata, dict):
        sched.extra_metadata = json.dumps(sched.extra_metadata)
    db.add(sched)
    db.commit()
    db.refresh(sched)
    return sched


def get_schedules(
    db: Session,
    created_by: Optional[int] = None,
    page: int = 1,
    limit: int = 25,
) -> Tuple[list, int]:
    query = db.query(NotificationSchedule)
    if created_by:
        query = query.filter(NotificationSchedule.created_by == created_by)
    query = query.order_by(NotificationSchedule.scheduled_at.desc())
    total = query.count()
    offset = (page - 1) * limit
    schedules = query.offset(offset).limit(limit).all()
    return schedules, total


def cancel_schedule(db: Session, schedule_id: int) -> bool:
    sched = db.query(NotificationSchedule).filter(
        NotificationSchedule.id == schedule_id,
        NotificationSchedule.status == "pending",
    ).first()
    if not sched:
        return False
    sched.status = "cancelled"
    db.commit()
    return True


def get_pending_schedules(db: Session, now: datetime) -> List[NotificationSchedule]:
    return (
        db.query(NotificationSchedule)
        .filter(
            NotificationSchedule.status == "pending",
            NotificationSchedule.scheduled_at <= now,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# Event Registry
# ---------------------------------------------------------------------------

def get_event_config(db: Session, event_name: str) -> Optional[NotificationEvent]:
    return db.query(NotificationEvent).filter(
        NotificationEvent.event_name == event_name,
        NotificationEvent.is_enabled == 1,
    ).first()


# ---------------------------------------------------------------------------
# Expired Banners
# ---------------------------------------------------------------------------

def deactivate_expired_banners(db: Session) -> List[int]:
    """Deactivate expired banners. Returns list of deactivated notification IDs."""
    now = datetime.utcnow()
    expired = (
        db.query(Notification)
        .filter(
            Notification.delivery_mode == "banner",
            Notification.is_active == 1,
            Notification.expires_at.isnot(None),
            Notification.expires_at <= now,
        )
        .all()
    )
    ids = []
    for banner in expired:
        banner.is_active = 0
        ids.append(banner.id)
    if ids:
        db.commit()
        logger.info("Deactivated %d expired banners: %s", len(ids), ids)
    return ids


# ---------------------------------------------------------------------------
# Job Deadline Helpers
# ---------------------------------------------------------------------------

def get_jobs_with_deadline_today(db: Session, today) -> list:
    """Jobs whose deadline is today (exceeded)."""
    return (
        db.query(JobOpenings)
        .filter(
            JobOpenings.deadline == today,
            JobOpenings.status == "ACTIVE",
            JobOpenings.deleted_at.is_(None),
        )
        .all()
    )


def get_jobs_with_deadline_approaching(db: Session, target_date) -> list:
    """Jobs whose deadline is exactly target_date (approaching in N days)."""
    return (
        db.query(JobOpenings)
        .filter(
            JobOpenings.deadline == target_date,
            JobOpenings.status == "ACTIVE",
            JobOpenings.deleted_at.is_(None),
        )
        .all()
    )
