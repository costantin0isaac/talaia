"""Password hashing.

Argon2id with the library's defaults, which are chosen to be slow on purpose: a stolen
``users`` table should be expensive to attack offline.
"""

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """Raised when a password is too short to accept."""


def hash_password(password: str) -> str:
    """Hash a password for storage.

    Raises:
        WeakPasswordError: The password is shorter than ``MIN_PASSWORD_LENGTH``.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        msg = f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        raise WeakPasswordError(msg)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Check a password against a stored hash, without raising on a mismatch."""
    try:
        return _hasher.verify(password_hash, password)
    except Argon2Error, InvalidHashError:
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether a stored hash predates the current parameters and should be replaced."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Argon2Error, InvalidHashError:
        return False
