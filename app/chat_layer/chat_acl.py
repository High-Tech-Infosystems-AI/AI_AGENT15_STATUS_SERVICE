"""RBAC enforcement for chat operations.

These are pure functions — no DB calls. Callers fetch the inputs first
(membership flags, role name, peer active flag) and pass them in.
"""
from datetime import datetime, timedelta
from typing import Optional

# Tolerate both naming conventions: the auth service returns lowercase
# snake_case ("super_admin"), older code used CamelCase ("SuperAdmin").
ADMIN_ROLES = {"super_admin", "admin", "SuperAdmin", "Admin"}
EDIT_WINDOW = timedelta(minutes=15)


def is_admin(role_name: Optional[str]) -> bool:
    if not role_name:
        return False
    return role_name in ADMIN_ROLES or role_name.lower() in {"super_admin", "admin"}


def can_post_dm(peer_active: bool) -> bool:
    return bool(peer_active)


def can_post_team(role_name: Optional[str], is_member: bool) -> bool:
    return is_admin(role_name) or bool(is_member)


def can_post_general() -> bool:
    return True


def can_forward_to_conversation(is_member_of_destination: bool) -> bool:
    return bool(is_member_of_destination)


def can_edit_message(sender_id: int, caller_id: int, created_at: datetime,
                     now: Optional[datetime] = None) -> bool:
    if sender_id != caller_id:
        return False
    if now is None:
        now = datetime.utcnow()
    return (now - created_at) <= EDIT_WINDOW


def can_delete_message(role_name: Optional[str]) -> bool:
    return is_admin(role_name)


def can_read_conversation(is_member: bool) -> bool:
    return bool(is_member)
