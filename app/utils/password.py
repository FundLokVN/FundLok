import logging

from passlib.context import CryptContext


pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    """True only when `plain_password` matches a real argon2 hash.

    A missing hash, or a stored value that isn't a hash at all, is a failed
    match rather than an error. Google/Microsoft sign-ups made before
    2026-08-09 (migration d5a8c31f6b02) stored a raw token_urlsafe(32)
    placeholder in users.password_hash; passlib raises UnknownHashError on it,
    which turned a wrong-password login into a 500 instead of a 401.
    """
    if not hashed_password:
        return False
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        return False
