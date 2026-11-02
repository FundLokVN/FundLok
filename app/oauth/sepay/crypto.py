"""Encryption at rest for SePay OAuth tokens.

A SePay access or refresh token reads the user's bank accounts and
transactions, so it is a credential. It must be replayed to SePay, which rules
out hashing. It is encrypted with Fernet, the same scheme and `v1:` prefix as
app/auth/totp_crypto.py, under its own key, SEPAY_TOKEN_ENCRYPTION_KEY.
Without a key nothing is stored: fail closed, never plaintext.
"""

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "v1:"


class EncryptionUnavailable(Exception):
    """No usable SEPAY_TOKEN_ENCRYPTION_KEY."""


def _cipher() -> Fernet:
    key = settings.SEPAY_TOKEN_ENCRYPTION_KEY
    if not key:
        logger.error("SEPAY_TOKEN_ENCRYPTION_KEY is unset: SePay tokens cannot be stored.")
        raise EncryptionUnavailable
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        logger.error("SEPAY_TOKEN_ENCRYPTION_KEY is not a valid Fernet key.")
        raise EncryptionUnavailable from exc


def encrypt_token(token: str) -> str:
    return _PREFIX + _cipher().encrypt(token.encode()).decode()


def decrypt_token(stored: str | None) -> str | None:
    """The plaintext token, or None when it cannot be recovered (other format,
    tampered row, rotated key)."""
    if not stored or not stored.startswith(_PREFIX):
        return None
    cipher = _cipher()
    try:
        return cipher.decrypt(stored[len(_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        return None
