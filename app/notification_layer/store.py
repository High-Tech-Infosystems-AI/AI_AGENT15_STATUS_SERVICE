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

    # By default, exclude 'log' notifications from the user-facing list.
    # Logs are audit-trail only — they must NOT appear in the bell/list either,
    # otherwise the list shows "unread" items that don't increment the badge
    # (get_unread_count also excludes logs). Honor explicit delivery_mode filters.
    if delivery_mode:
        query = query.filter(Notification.delivery_mode == delivery_mode)
    else:
        query = query.filter(Notification.delivery_mode != "log")

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

    total = query.count()

    # Unread count scoped to the same filter set as the list
    # (so list and unread are always consistent).
    unread_query = (
        db.query(func.count(NotificationRecipient.id))
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .filter(
            NotificationRecipient.user_id == user_id,
            NotificationRecipient.is_read == 0,
            Notification.is_active == 1,
        )
    )
    if delivery_mode:
        unread_query = unread_query.filter(Notification.delivery_mode == delivery_mode)
    else:
        # Default list excludes logs → unread count here matches
        unread_query = unread_query.filter(Notification.delivery_mode != "log")
    if domain_type:
        unread_query = unread_query.filter(Notification.domain_type.in_([t.strip() for t in domain_type.split(",")]))
    unread = unread_query.scalar() or 0

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

def _parse_csv_ints(value: Optional[str]) -> List[int]:
    """Parse a CSV string of integers. Returns empty list for None/empty."""
    if not value:
        return []
    out = []
    for part in value.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


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
    user_id: Optional[str] = None,      # now CSV string: "1,5,12"
    job_id: Optional[str] = None,       # CSV: "42,43"
    company_id: Optional[str] = None,   # CSV: "7,8"
    created_by: Optional[str] = None,   # now CSV string: "1,2"
    delivery_mode: Optional[str] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    include_not_received: bool = False,  # expensive N+1 resolution, opt-in
) -> Tuple[list, int]:
    """
    Admin view — returns all notifications with recipient stats.
    `user_id`, `job_id`, `company_id`, `created_by` accept comma-separated values.
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
        etypes = [e.strip() for e in event_type.split(",") if e.strip()]
        if etypes:
            query = query.filter(Notification.event_type.in_(etypes))
    if delivery_mode:
        modes = [m.strip() for m in delivery_mode.split(",") if m.strip()]
        query = query.filter(Notification.delivery_mode.in_(modes))

    # Created-by (CSV user IDs)
    created_by_ids = _parse_csv_ints(created_by)
    if created_by_ids:
        query = query.filter(Notification.created_by.in_(created_by_ids))

    # Recipient user filter (CSV user IDs)
    user_ids = _parse_csv_ints(user_id)
    if user_ids:
        query = query.filter(
            Notification.id.in_(
                db.query(NotificationRecipient.notification_id)
                .filter(NotificationRecipient.user_id.in_(user_ids))
                .subquery()
            )
        )

    # Job-id filter: match notifications whose target_type='job' and target_id is in list,
    # OR whose metadata JSON contains a matching job_id.
    job_ids = _parse_csv_ints(job_id)
    if job_ids:
        job_id_strs = [str(j) for j in job_ids]
        # JSON LIKE clauses for metadata.job_id (handles both numeric and string storage)
        json_likes = [
            Notification.extra_metadata.like(f'%"job_id": {j}%') for j in job_ids
        ] + [
            Notification.extra_metadata.like(f'%"job_id": "{j}"%') for j in job_ids
        ]
        query = query.filter(
            or_(
                and_(Notification.target_type == "job", Notification.target_id.in_(job_id_strs)),
                *json_likes,
            )
        )

    # Company-id filter: metadata JSON contains company_id
    company_ids = _parse_csv_ints(company_id)
    if company_ids:
        company_likes = [
            Notification.extra_metadata.like(f'%"company_id": {c}%') for c in company_ids
        ] + [
            Notification.extra_metadata.like(f'%"company_id": "{c}"%') for c in company_ids
        ]
        query = query.filter(or_(*company_likes))

    total = query.count()

    sort_col = getattr(Notification, sort_by, Notification.created_at)
    order = sort_col.desc() if sort_order == "desc" else sort_col.asc()
    query = query.order_by(order)

    offset = (page - 1) * limit
    notifications = query.offset(offset).limit(limit).all()
    notif_ids = [n.id for n in notifications]

    # Bulk fetch all recipients (with user name) for the paginated notifications in one query.
    recipients_by_notif: dict = {nid: [] for nid in notif_ids}
    if notif_ids:
        rows = (
            db.query(
                NotificationRecipient.notification_id,
                NotificationRecipient.user_id,
                NotificationRecipient.is_read,
                NotificationRecipient.read_at,
                User.name,
            )
            .join(User, User.id == NotificationRecipient.user_id)
            .filter(NotificationRecipient.notification_id.in_(notif_ids))
            .all()
        )
        for nid, uid, is_read, read_at, uname in rows:
            recipients_by_notif.setdefault(nid, []).append({
                "user_id": uid,
                "user_name": uname,
                "is_read": bool(is_read),
                "read_at": read_at,
            })

    # Bulk fetch creator names for all notifications in one query
    creator_ids = {n.created_by for n in notifications if n.created_by}
    creator_names: dict = {}
    if creator_ids:
        for uid, uname in db.query(User.id, User.name).filter(User.id.in_(creator_ids)).all():
            creator_names[uid] = uname

    results = []
    for notif in notifications:
        recipients = recipients_by_notif.get(notif.id, [])

        # Partition into read / unread lists
        read_users = [r for r in recipients if r["is_read"]]
        unread_users = [r for r in recipients if not r["is_read"]]

        # "Not received" resolution is expensive (runs role/job resolution per notification).
        # Only compute it when explicitly requested via ?include_not_received=true.
        not_received_users = []
        if include_not_received:
            try:
                intended_ids = set(resolve_target_user_ids(
                    db, notif.target_type, notif.target_id,
                    include_admins=(notif.target_type != "all"),
                ))
                recipient_ids_set = {r["user_id"] for r in recipients}
                not_received_ids = list(intended_ids - recipient_ids_set)
                if not_received_ids:
                    nr_rows = db.query(User.id, User.name).filter(User.id.in_(not_received_ids)).all()
                    not_received_users = [{"user_id": uid, "user_name": uname} for uid, uname in nr_rows]
            except Exception:
                pass

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
            "created_by_name": creator_names.get(notif.created_by),
            "created_at": notif.created_at,
            "expires_at": notif.expires_at,
            "is_active": bool(notif.is_active),

            # Summary counts
            "recipients_count": len(recipients),
            "read_count": len(read_users),
            "unread_count": len(unread_users),
            "not_received_count": len(not_received_users),

            # Detailed per-user breakdown
            "read_by": read_users,              # [{user_id, user_name, is_read=True, read_at}]
            "unread_by": unread_users,          # [{user_id, user_name, is_read=False, read_at=None}]
            "not_received_by": not_received_users,  # [{user_id, user_name}]
        })

    return results, total


# ---------------------------------------------------------------------------
# Unread Count
# ---------------------------------------------------------------------------

def get_unread_count(db: Session, user_id: int) -> int:
    """Unread count for the main notification badge (push only).
    Logs and banners have their own badges via get_unread_counts_by_mode().
    """
    return (
        db.query(func.count(NotificationRecipient.id))
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .filter(
            NotificationRecipient.user_id == user_id,
            NotificationRecipient.is_read == 0,
            Notification.is_active == 1,
            Notification.delivery_mode != "log",
        )
        .scalar()
    ) or 0


def get_unread_counts_by_mode(db: Session, user_id: int) -> dict:
    """Return per-delivery-mode unread counts for a user.
    {"push": N, "banner": N, "log": N, "total": N}
    """
    rows = (
        db.query(Notification.delivery_mode, func.count(NotificationRecipient.id))
        .join(NotificationRecipient, Notification.id == NotificationRecipient.notification_id)
        .filter(
            NotificationRecipient.user_id == user_id,
            NotificationRecipient.is_read == 0,
            Notification.is_active == 1,
        )
        .group_by(Notification.delivery_mode)
        .all()
    )
    counts = {"push": 0, "banner": 0, "log": 0}
    total = 0
    for mode, cnt in rows:
        counts[mode] = cnt
        total += cnt
    counts["total"] = total
    return counts


def get_unread_counts_bulk(db: Session, user_ids: List[int]) -> dict:
    """Return {user_id: unread_count} for a batch of users in a single query.
    Count excludes logs (matches the main notification badge)."""
    if not user_ids:
        return {}
    rows = (
        db.query(NotificationRecipient.user_id, func.count(NotificationRecipient.id))
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .filter(
            NotificationRecipient.user_id.in_(user_ids),
            NotificationRecipient.is_read == 0,
            Notification.is_active == 1,
            Notification.delivery_mode != "log",
        )
        .group_by(NotificationRecipient.user_id)
        .all()
    )
    counts = {uid: 0 for uid in user_ids}
    for uid, cnt in rows:
        counts[uid] = cnt
    return counts


def get_unread_counts_by_mode_bulk(db: Session, user_ids: List[int]) -> dict:
    """Return {user_id: {"push": N, "banner": N, "log": N, "total": N}} for a batch."""
    if not user_ids:
        return {}
    rows = (
        db.query(NotificationRecipient.user_id, Notification.delivery_mode,
                 func.count(NotificationRecipient.id))
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .filter(
            NotificationRecipient.user_id.in_(user_ids),
            NotificationRecipient.is_read == 0,
            Notification.is_active == 1,
        )
        .group_by(NotificationRecipient.user_id, Notification.delivery_mode)
        .all()
    )
    result = {uid: {"push": 0, "banner": 0, "log": 0, "total": 0} for uid in user_ids}
    for uid, mode, cnt in rows:
        if uid in result:
            result[uid][mode] = cnt
            result[uid]["total"] += cnt
    return result


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


def mark_notification_unread(db: Session, notification_id: int, user_id: int) -> bool:
    """Mark a notification as unread for the given user. Returns True if updated."""
    recipient = db.query(NotificationRecipient).filter(
        NotificationRecipient.notification_id == notification_id,
        NotificationRecipient.user_id == user_id,
    ).first()
    if not recipient:
        return False
    recipient.is_read = 0
    recipient.read_at = None
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Active Banners
# ---------------------------------------------------------------------------

def get_active_banners(db: Session) -> list:
    """All currently-active banners (admin/global view, no user filtering)."""
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
            "visibility": b.visibility,
            "target_type": b.target_type,
            "expires_at": b.expires_at,
            "created_at": b.created_at,
            "metadata": meta,
        })
    return results


def get_active_banners_for_user(db: Session, user_id: int) -> list:
    """
    Active banners visible to a specific user.
    Joins notification_recipients so users only see banners they were targeted by
    (which includes 'all' broadcasts since those create a recipient row per user).
    """
    rows = (
        db.query(Notification)
        .join(NotificationRecipient, Notification.id == NotificationRecipient.notification_id)
        .filter(
            Notification.delivery_mode == "banner",
            Notification.is_active == 1,
            NotificationRecipient.user_id == user_id,
            or_(
                Notification.expires_at.is_(None),
                Notification.expires_at > datetime.utcnow(),
            ),
        )
        .order_by(Notification.created_at.desc())
        .all()
    )
    results = []
    for b in rows:
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
            "visibility": b.visibility,
            "target_type": b.target_type,
            "expires_at": b.expires_at,
            "created_at": b.created_at,
            "metadata": meta,
        })
    return results


def get_active_banners_for_users_bulk(db: Session, user_ids: List[int]) -> dict:
    """
    Bulk query: returns {user_id: [banner_dict, ...]} for each user_id provided.
    One SQL round-trip joining notifications + notification_recipients,
    then grouped in Python. Used to publish per-user banner snapshots after
    create/expire events.
    """
    if not user_ids:
        return {}

    rows = (
        db.query(NotificationRecipient.user_id, Notification)
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .filter(
            Notification.delivery_mode == "banner",
            Notification.is_active == 1,
            NotificationRecipient.user_id.in_(user_ids),
            or_(
                Notification.expires_at.is_(None),
                Notification.expires_at > datetime.utcnow(),
            ),
        )
        .order_by(Notification.created_at.desc())
        .all()
    )

    result = {uid: [] for uid in user_ids}
    for uid, b in rows:
        meta = None
        if b.extra_metadata:
            try:
                meta = json.loads(b.extra_metadata)
            except (json.JSONDecodeError, TypeError):
                meta = b.extra_metadata
        result.setdefault(uid, []).append({
            "id": b.id,
            "title": b.title,
            "message": b.message,
            "priority": b.priority,
            "domain_type": b.domain_type,
            "visibility": b.visibility,
            "target_type": b.target_type,
            "expires_at": str(b.expires_at) if b.expires_at else None,
            "created_at": str(b.created_at) if b.created_at else None,
            "metadata": meta,
        })
    return result


def get_banner_recipient_ids(db: Session, banner_id: int) -> List[int]:
    """Return user_ids of all recipients of a specific banner. Used for expire fan-out."""
    rows = (
        db.query(NotificationRecipient.user_id)
        .filter(NotificationRecipient.notification_id == banner_id)
        .all()
    )
    return [r[0] for r in rows]


def deactivate_expired_banners_with_recipients(db: Session) -> list:
    """Deactivate expired banners and return [(banner_id, [recipient_user_ids]), ...]."""
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
    result = []
    for banner in expired:
        recipient_ids = get_banner_recipient_ids(db, banner.id)
        banner.is_active = 0
        result.append((banner.id, recipient_ids))
    if result:
        db.commit()
        logger.info("Deactivated %d expired banners", len(result))
    return result


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


def get_schedule_by_id(db: Session, schedule_id: int) -> Optional[NotificationSchedule]:
    return db.query(NotificationSchedule).filter(NotificationSchedule.id == schedule_id).first()


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


def update_schedule(db: Session, schedule_id: int, updates: dict) -> Optional[NotificationSchedule]:
    """Update a pending schedule and return it; None if not editable/not found."""
    sched = db.query(NotificationSchedule).filter(
        NotificationSchedule.id == schedule_id,
        NotificationSchedule.status == "pending",
    ).first()
    if not sched:
        return None

    for field_name, field_value in updates.items():
        setattr(sched, field_name, field_value)

    db.commit()
    db.refresh(sched)
    return sched


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


# ---------------------------------------------------------------------------
# Admin Stats & Management
# ---------------------------------------------------------------------------

def get_admin_stats(
    db: Session,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict:
    """
    Aggregate stats for the admin dashboard, optionally date-filtered.

    Returns:
        total_notifications_sent: int
        notifications_scheduled: int   (pending schedules)
        engagement_rate: float   (% of delivered recipients that read the notification)
        delivery_success: float  (% of notifications that reached at least one recipient)
    """
    # --- Total notifications sent (all delivery modes, only active + in date range)
    notif_q = db.query(Notification).filter(Notification.is_active == 1)
    if date_from:
        notif_q = notif_q.filter(Notification.created_at >= date_from)
    if date_to:
        notif_q = notif_q.filter(Notification.created_at <= date_to)
    total_notifications_sent = notif_q.count()

    # --- Pending scheduled notifications (date-filtered by scheduled_at if provided)
    sched_q = db.query(NotificationSchedule).filter(NotificationSchedule.status == "pending")
    if date_from:
        sched_q = sched_q.filter(NotificationSchedule.scheduled_at >= date_from)
    if date_to:
        sched_q = sched_q.filter(NotificationSchedule.scheduled_at <= date_to)
    notifications_scheduled = sched_q.count()

    # --- Engagement rate: sum(read) / sum(recipients) * 100
    recipients_stats = (
        db.query(
            func.count(NotificationRecipient.id).label("total"),
            func.sum(case((NotificationRecipient.is_read == 1, 1), else_=0)).label("read"),
        )
        .join(Notification, Notification.id == NotificationRecipient.notification_id)
        .filter(Notification.is_active == 1)
    )
    if date_from:
        recipients_stats = recipients_stats.filter(Notification.created_at >= date_from)
    if date_to:
        recipients_stats = recipients_stats.filter(Notification.created_at <= date_to)
    row = recipients_stats.one()
    total_recipients = row.total or 0
    total_read = int(row.read or 0)
    engagement_rate = round((total_read / total_recipients) * 100, 2) if total_recipients > 0 else 0.0

    # --- Delivery success: % of notifications that reached at least one recipient
    if total_notifications_sent > 0:
        # Subquery: notification IDs that have >=1 recipient
        notif_with_recipients = (
            db.query(NotificationRecipient.notification_id)
            .distinct()
            .subquery()
        )
        delivered_q = db.query(Notification).filter(
            Notification.is_active == 1,
            Notification.id.in_(db.query(notif_with_recipients.c.notification_id)),
        )
        if date_from:
            delivered_q = delivered_q.filter(Notification.created_at >= date_from)
        if date_to:
            delivered_q = delivered_q.filter(Notification.created_at <= date_to)
        delivered_count = delivered_q.count()
        delivery_success = round((delivered_count / total_notifications_sent) * 100, 2)
    else:
        delivery_success = 0.0

    return {
        "total_notifications_sent": total_notifications_sent,
        "notifications_scheduled": notifications_scheduled,
        "engagement_rate": engagement_rate,
        "delivery_success": delivery_success,
        "total_recipients": total_recipients,
        "total_read": total_read,
    }


def update_banner_expiry(
    db: Session, banner_id: int, new_expires_at: Optional[datetime]
) -> Optional[Notification]:
    """Update the expiry date of an existing banner. Returns the banner or None if not found."""
    banner = (
        db.query(Notification)
        .filter(
            Notification.id == banner_id,
            Notification.delivery_mode == "banner",
        )
        .first()
    )
    if not banner:
        return None
    banner.expires_at = new_expires_at
    # If expiry is in the past, also deactivate
    if new_expires_at and new_expires_at <= datetime.utcnow():
        banner.is_active = 0
    else:
        banner.is_active = 1
    db.commit()
    db.refresh(banner)
    return banner


def expire_banner_now(db: Session, banner_id: int) -> Optional[Tuple[Notification, List[int]]]:
    """Expire a banner immediately. Returns (banner, recipient_ids) or None if not found."""
    banner = (
        db.query(Notification)
        .filter(
            Notification.id == banner_id,
            Notification.delivery_mode == "banner",
        )
        .first()
    )
    if not banner:
        return None
    banner.is_active = 0
    banner.expires_at = datetime.utcnow()
    recipient_ids = get_banner_recipient_ids(db, banner_id)
    db.commit()
    return banner, recipient_ids


def get_notification_recipient_ids(db: Session, notification_id: int) -> List[int]:
    """Return the list of user_ids who were recipients of this notification."""
    rows = (
        db.query(NotificationRecipient.user_id)
        .filter(NotificationRecipient.notification_id == notification_id)
        .all()
    )
    return [r[0] for r in rows]


def soft_delete_notification(db: Session, notification_id: int) -> Optional[Notification]:
    """Soft-delete a notification (sets is_active=0). Returns the notification or None."""
    notif = db.query(Notification).filter(Notification.id == notification_id).first()
    if not notif:
        return None
    notif.is_active = 0
    db.commit()
    db.refresh(notif)
    return notif
