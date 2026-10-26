"""Recipient lookups on the shared `users` table (owned by RBAC).

Phone numbers come from `users.phone_country_code` + `users.phone_number`
(contract §3.3); `users.enable` / `deleted_at` decide `recipient_disabled`.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

_FULL_SQL = text(
    "SELECT id, name, email, organization_id, phone_country_code, phone_number, "
    "enable, deleted_at FROM users WHERE id IN :ids"
).bindparams(bindparam("ids", expanding=True))

# Fallback if the RBAC phone migration hasn't run on this DB yet.
_BASIC_SQL = text(
    "SELECT id, name, email, organization_id, NULL, NULL, 1, deleted_at "
    "FROM users WHERE id IN :ids"
).bindparams(bindparam("ids", expanding=True))


def to_e164(country_code, number) -> Optional[str]:
    """`+91` / `91` + `98765 43210` → `+919876543210`; None if unusable.

    A number already given with a leading '+' is taken as complete."""
    raw_number = str(number or "").strip()
    if not raw_number:
        return None
    if raw_number.startswith("+"):
        digits = re.sub(r"\D", "", raw_number)
    else:
        cc = re.sub(r"\D", "", str(country_code or ""))
        nd = re.sub(r"\D", "", raw_number).lstrip("0")
        if not cc or not nd:
            return None
        digits = nd if nd.startswith(cc) and len(nd) > 10 else cc + nd
    if not 8 <= len(digits) <= 15:
        return None
    return f"+{digits}"


def get_users(db: Session, user_ids: Iterable[int]) -> Dict[int, Dict]:
    """{user_id: {id, name, email, organization_id, phone_e164, enabled}}."""
    ids = sorted({int(i) for i in user_ids if i})
    if not ids:
        return {}
    try:
        rows = db.execute(_FULL_SQL, {"ids": ids}).fetchall()
    except Exception as exc:
        logger.warning("users phone columns unavailable (%s); phones treated as missing", exc)
        db.rollback()
        rows = db.execute(_BASIC_SQL, {"ids": ids}).fetchall()
    out: Dict[int, Dict] = {}
    for r in rows:
        enable = r[6]
        out[int(r[0])] = {
            "id": int(r[0]),
            "name": r[1],
            "email": r[2],
            "organization_id": r[3],
            "phone_e164": to_e164(r[4], r[5]),
            "enabled": (enable is None or int(enable) == 1) and r[7] is None,
        }
    return out


def user_names(db: Session, user_ids: Iterable[int]) -> Dict[int, str]:
    ids = sorted({int(i) for i in user_ids if i})
    if not ids:
        return {}
    rows = db.execute(text("SELECT id, name FROM users WHERE id IN :ids")
                      .bindparams(bindparam("ids", expanding=True)), {"ids": ids}).fetchall()
    return {int(r[0]): r[1] for r in rows}
