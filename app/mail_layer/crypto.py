"""Symmetric encryption for stored mailbox credentials.

SMTP/IMAP passwords are never stored in plaintext. They are encrypted with
Fernet (AES-128-CBC + HMAC) using ``MAIL_ENCRYPTION_KEY`` — a urlsafe-base64
32-byte key held in a K8s Secret, never in the DB or ConfigMap.

The ciphertext is stored in ``mail_accounts.smtp_password_enc`` /
``imap_password_enc`` (BLOB). No API ever returns the plaintext.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core import settings

logger = logging.getLogger("app_logger")

_fernet: Optional[Fernet] = None


def _key() -> bytes:
    raw = getattr(settings, "MAIL_ENCRYPTION_KEY", "") or os.getenv("MAIL_ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError(
            "MAIL_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and set it as a secret."
        )
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return raw


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_key())
    return _fernet


def encrypt(plaintext: str) -> bytes:
    """Encrypt a password to ciphertext bytes for DB storage."""
    if plaintext is None:
        plaintext = ""
    return _cipher().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext) -> str:
    """Decrypt stored ciphertext back to the plaintext password.

    Accepts bytes (BLOB) or str. Raises on tampered/undecryptable data so the
    sync/send workers surface a clear error instead of silently using garbage.
    """
    if ciphertext is None:
        return ""
    if isinstance(ciphertext, str):
        ciphertext = ciphertext.encode("utf-8")
    try:
        return _cipher().decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt a stored mail credential: %s", exc)
        raise RuntimeError("Stored credential could not be decrypted") from exc
