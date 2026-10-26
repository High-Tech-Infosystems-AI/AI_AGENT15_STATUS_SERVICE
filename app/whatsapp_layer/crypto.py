"""Symmetric encryption for stored WhatsApp provider tokens.

Provider access tokens are never stored in plaintext. They are encrypted with
Fernet using ``WHATSAPP_CRED_KEY`` — a urlsafe-base64 32-byte key held in a
K8s Secret. The ciphertext lives in
``whatsapp_provider_accounts.access_token_enc``; the API only ever returns
the last 4 characters (``token_last4``). Tokens are never logged.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("app_logger")

_fernet: Optional[Fernet] = None
_fernet_key: Optional[bytes] = None


def _key() -> bytes:
    from app.core import settings
    raw = getattr(settings, "WHATSAPP_CRED_KEY", "") or os.getenv("WHATSAPP_CRED_KEY", "")
    if not raw:
        raise RuntimeError(
            "WHATSAPP_CRED_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and set it as a secret."
        )
    return raw.encode("utf-8") if isinstance(raw, str) else raw


def _cipher() -> Fernet:
    global _fernet, _fernet_key
    key = _key()
    if _fernet is None or key != _fernet_key:
        _fernet = Fernet(key)
        _fernet_key = key
    return _fernet


def encrypt(plaintext: str) -> bytes:
    """Encrypt a provider token to ciphertext bytes for DB storage."""
    return _cipher().encrypt((plaintext or "").encode("utf-8"))


def decrypt(ciphertext) -> str:
    """Decrypt stored ciphertext. Raises on tampered data / wrong key so the
    sender surfaces a clear error instead of calling the provider with garbage."""
    if ciphertext is None:
        return ""
    if isinstance(ciphertext, str):
        ciphertext = ciphertext.encode("utf-8")
    try:
        return _cipher().decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt a stored WhatsApp provider token")
        raise RuntimeError("Stored WhatsApp token could not be decrypted") from exc


def last4(token: Optional[str]) -> Optional[str]:
    token = (token or "").strip()
    return token[-4:] if token else None
