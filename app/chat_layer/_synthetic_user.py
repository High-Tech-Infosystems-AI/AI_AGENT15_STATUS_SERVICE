"""Schema-aware provisioning for synthetic bot users (status_bot, ai_assistant).

The `users` table is owned by another service and its exact column set varies
between deployments — some use `password`, others `password_hash`, some have
extra NOT-NULL columns with no defaults (`first_name`, `created_by`, …). A
hardcoded INSERT therefore breaks the moment the schema drifts.

`provision_synthetic_user` introspects `information_schema.columns` once,
builds an INSERT that only targets columns that exist, and supplies a safe
placeholder for every NOT-NULL column without a default. Both bots reuse it.
"""
from __future__ import annotations

import logging
from threading import Lock
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("app_logger")

# Per-process cache of the users-table column metadata. The schema is
# stable for the lifetime of the process; refreshing on every call would
# add a round-trip per request without buying anything.
_USERS_COLS_CACHE: Optional[Dict[str, dict]] = None
_CACHE_LOCK = Lock()

# Column-name aliases for "the password field". Whichever of these the
# users table happens to have, we'll fill it with a non-empty placeholder.
_PASSWORD_ALIASES = {
    "password", "password_hash", "passwd", "hashed_password", "pwd",
}

# Numeric types we feel safe defaulting to 0 for unaddressed NOT-NULL cols.
_NUMERIC_TYPES = {
    "int", "tinyint", "smallint", "mediumint", "bigint",
    "decimal", "float", "double", "numeric",
}
# Date/time types — fall back to NOW() at runtime.
_DATETIME_TYPES = {"datetime", "timestamp", "date", "time"}


def _load_columns(db: Session) -> Dict[str, dict]:
    """Return {column_name_lower: {data_type, is_nullable, has_default}}."""
    global _USERS_COLS_CACHE
    if _USERS_COLS_CACHE is not None:
        return _USERS_COLS_CACHE
    with _CACHE_LOCK:
        if _USERS_COLS_CACHE is not None:
            return _USERS_COLS_CACHE
        rows = db.execute(text("""
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                   COLUMN_DEFAULT, EXTRA
              FROM information_schema.columns
             WHERE table_schema = DATABASE()
               AND table_name   = 'users'
        """)).all()
        cols: Dict[str, dict] = {}
        for r in rows:
            m = r._mapping
            name = (m["COLUMN_NAME"] or "").lower()
            cols[name] = {
                "name": m["COLUMN_NAME"],
                "type": (m["DATA_TYPE"] or "").lower(),
                "nullable": (m["IS_NULLABLE"] or "YES").upper() == "YES",
                "has_default": m["COLUMN_DEFAULT"] is not None,
                "extra": (m["EXTRA"] or "").lower(),
            }
        _USERS_COLS_CACHE = cols
        return cols


def _placeholder_for(col: dict, name_hint: str) -> object:
    """Pick a safe placeholder value for a NOT-NULL column we didn't set."""
    nm = name_hint.lower()
    t = col["type"]
    if nm in _PASSWORD_ALIASES or "password" in nm:
        return "!disabled!"
    if t in _NUMERIC_TYPES:
        return 0
    if t in _DATETIME_TYPES:
        # Use a SQL function via raw text so MySQL fills NOW() at INSERT.
        return "__NOW__"  # sentinel handled below
    # Strings / blobs / JSON / enum — empty string is broadly accepted.
    return ""


def provision_synthetic_user(
    db: Session,
    *,
    username: str,
    display_name: str,
    email: str,
) -> int:
    """Insert a synthetic, login-disabled user and return its id.

    Idempotent: if a row with the given username already exists, returns its
    id without inserting. Adapts the INSERT to the live `users` schema so
    NOT-NULL columns without defaults receive sane placeholders.
    """
    existing = db.execute(
        text("SELECT id FROM users WHERE username = :u LIMIT 1"),
        {"u": username},
    ).first()
    if existing:
        return int(existing[0])

    cols = _load_columns(db)

    # --- 1. Values we know we want, only kept if the column exists. ---
    desired: Dict[str, object] = {}
    if "name" in cols:
        desired["name"] = display_name
    if "username" in cols:
        desired["username"] = username
    if "email" in cols:
        desired["email"] = email
    if "enable" in cols:
        desired["enable"] = 0
    if "is_active" in cols:
        desired["is_active"] = 0
    if "deleted_at" in cols:
        desired["deleted_at"] = None
    if "created_at" in cols:
        desired["created_at"] = "__NOW__"
    if "updated_at" in cols:
        desired["updated_at"] = "__NOW__"
    # Whatever password-like column the schema uses, fill it.
    for alias in _PASSWORD_ALIASES:
        if alias in cols:
            desired[alias] = "!disabled!"
    # First / last name splits — populate from display_name halves.
    if "first_name" in cols:
        desired["first_name"] = display_name.split(" ", 1)[0] or display_name
    if "last_name" in cols:
        parts = display_name.split(" ", 1)
        desired["last_name"] = parts[1] if len(parts) > 1 else ""

    # --- 2. Backfill any NOT-NULL column without default we didn't address. ---
    for col_name_lower, col in cols.items():
        if col_name_lower in desired:
            continue
        if col["nullable"] or col["has_default"]:
            continue
        if "auto_increment" in col["extra"]:
            continue
        # NOT NULL, no default, not autoincrement, not yet supplied — must fill.
        desired[col_name_lower] = _placeholder_for(col, col_name_lower)

    if not desired:
        raise RuntimeError("users table has no columns we can insert into")

    # --- 3. Build the INSERT, expanding the __NOW__ sentinel to NOW(). ---
    columns = []
    placeholders = []
    params: Dict[str, object] = {}
    for k, v in desired.items():
        real_name = cols[k]["name"]
        columns.append(f"`{real_name}`")
        if v == "__NOW__":
            placeholders.append("NOW()")
        else:
            param_key = f"p_{k}"
            placeholders.append(f":{param_key}")
            params[param_key] = v

    sql = (
        f"INSERT INTO users ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)})"
    )
    logger.info("provisioning synthetic user '%s' with columns: %s",
                username, [c.strip("`") for c in columns])
    db.execute(text(sql), params)
    db.commit()

    row = db.execute(
        text("SELECT id FROM users WHERE username = :u LIMIT 1"),
        {"u": username},
    ).first()
    if not row:
        raise RuntimeError(f"synthetic user '{username}' insert returned no row")
    return int(row[0])


def reset_schema_cache() -> None:
    """For tests / migrations — drop the cached schema introspection."""
    global _USERS_COLS_CACHE
    with _CACHE_LOCK:
        _USERS_COLS_CACHE = None
