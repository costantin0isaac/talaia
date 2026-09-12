"""Session tokens.

A token is 256 bits of randomness, so it is only ever hashed with SHA-256: there is no
low-entropy secret to protect, and argon2 here would add its deliberate slowness to every
authenticated request for nothing. What matters is that the database stores the hash, so a
copy of the ``sessions`` table cannot be replayed as a set of live logins.
"""

import hashlib
import secrets

TOKEN_BYTES = 32


def new_token() -> str:
    """Mint a session token to hand to the browser."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the hex digest stored in place of the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
