"""SEC-07: hashing moved from passlib to argon2-cffi without breaking a hash.

PASSLIB_HASH was made by the passlib CryptContext this replaced, so this
proves every password stored before the switch still signs in.
"""
import pytest

from app.utils.password import hash_password, verify_password

PASSLIB_HASH = "$argon2id$v=19$m=65536,t=3,p=4$lVLqHUOIESIkRChFiDGm1A$j7FqV7Y9pEqcE9lsvbHt5IzGqJ1uPV7+l879bdK4xC8"
PASSLIB_PASSWORD = "Legacy-Passw0rd!"


def test_a_hash_made_by_passlib_still_verifies():
    assert verify_password(PASSLIB_PASSWORD, PASSLIB_HASH) is True
    assert verify_password("Wrong-Passw0rd!", PASSLIB_HASH) is False


def test_new_hashes_use_the_same_parameters_passlib_did():
    hashed = hash_password("Fresh-Passw0rd!")
    assert hashed.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert verify_password("Fresh-Passw0rd!", hashed) is True


@pytest.mark.parametrize("stored", [None, "", "not-a-hash-at-all", "$argon2id$v=19$garbled"])
def test_anything_that_is_not_a_hash_is_a_failed_match(stored):
    assert verify_password("Any-Passw0rd!", stored) is False
