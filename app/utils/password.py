from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# argon2-cffi directly, not through passlib, which is unmaintained (compliance
# audit SEC-07). Its defaults are the parameters passlib used (argon2id,
# m=64 MiB, t=3, p=4) and both write the standard PHC string, so every hash
# already stored verifies unchanged; tests/auth/test_password_hashing.py pins
# one made by passlib.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    """True only when `plain_password` matches a real argon2 hash.

    A missing hash, or a stored value that isn't a hash at all, is a failed
    match rather than an error. Google/Microsoft sign-ups made before
    2026-08-09 (migration d5a8c31f6b02) stored a raw token_urlsafe(32)
    placeholder in users.password_hash; reading it as a hash raises, which
    turned a wrong-password login into a 500 instead of a 401.
    """
    if not hashed_password:
        return False
    try:
        return _hasher.verify(hashed_password, plain_password)
    except (VerificationError, InvalidHashError):
        return False
