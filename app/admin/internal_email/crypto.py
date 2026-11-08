"""Encryption at rest for the Google OAuth refresh token behind each admin's
Gmail connection.

The refresh token is a long-lived credential: whoever holds it (and the OAuth
client secret) can mint access tokens and send email as that mailbox. The
server needs it back in plaintext to do exactly that, which rules out hashing,
so it is encrypted with Fernet, the same scheme as app/auth/totp_crypto.py,
including the `v1:` prefix that lets a later move to KMS envelope encryption
migrate rows one at a time.

What this protects against, and what it does not, is the same boundary as the
TOTP secrets: a stolen database (dump, backup, replica) is useless without the
key; an attacker who has the application's environment has the key too.

The key, EMAIL_SETTINGS_ENCRYPTION_KEY, is separate from SECRET_KEY and from
TOTP_ENCRYPTION_KEY, so rotating one never makes another's data unreadable.
Without a key no account can be connected: fail closed, never plaintext.
Rotating it makes every stored token unreadable, which the service treats as
"reconnect with Google".
"""

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "v1:"


class EncryptionUnavailable(Exception):
    """No usable EMAIL_SETTINGS_ENCRYPTION_KEY. The service turns this into a
    503 whose message does not name the server's configuration."""


def generate_encryption_key() -> str:
    """A fresh key, for `EMAIL_SETTINGS_ENCRYPTION_KEY`.

    python -c "from app.admin.internal_email.crypto import generate_encryption_key as g; print(g())"
    """
    return Fernet.generate_key().decode()


def _cipher() -> Fernet:
    key = settings.EMAIL_SETTINGS_ENCRYPTION_KEY
    if not key:
        logger.error(
            "EMAIL_SETTINGS_ENCRYPTION_KEY is unset: the Gmail OAuth token "
            "cannot be stored. Generate one with "
            "app.admin.internal_email.crypto.generate_encryption_key()."
        )
        raise EncryptionUnavailable
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        logger.error("EMAIL_SETTINGS_ENCRYPTION_KEY is not a valid Fernet key.")
        raise EncryptionUnavailable from exc


def encrypt_secret(secret: str) -> str:
    return _PREFIX + _cipher().encrypt(secret.encode()).decode()


def decrypt_secret(stored: str | None) -> str | None:
    """The plaintext secret, or None when it cannot be recovered: nothing
    stored, a value from another format, a tampered row, or a key that has
    since been rotated. Raises EncryptionUnavailable when no key is set at all,
    so that case is reported as a server problem rather than a bad token."""
    if not stored or not stored.startswith(_PREFIX):
        return None
    cipher = _cipher()
    try:
        return cipher.decrypt(stored[len(_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        return None
