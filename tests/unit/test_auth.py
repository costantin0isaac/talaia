"""Password hashing and session tokens."""

import pytest

from talaia.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    hash_password,
    needs_rehash,
    verify_password,
)
from talaia.auth.tokens import TOKEN_BYTES, hash_token, new_token

PASSWORD = "a-long-enough-password"
SHA256_HEX_LENGTH = 64


class TestHashPassword:
    def test_produces_an_argon2id_hash(self) -> None:
        assert hash_password(PASSWORD).startswith("$argon2id$")

    def test_the_same_password_hashes_differently_every_time(self) -> None:
        """Per-hash salts, so identical passwords are not identifiable in the table."""
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_the_password_is_not_recoverable_from_the_hash(self) -> None:
        assert PASSWORD not in hash_password(PASSWORD)

    def test_a_short_password_is_refused(self) -> None:
        with pytest.raises(WeakPasswordError):
            hash_password("x" * (MIN_PASSWORD_LENGTH - 1))

    def test_the_minimum_length_is_accepted(self) -> None:
        assert hash_password("x" * MIN_PASSWORD_LENGTH)


class TestVerifyPassword:
    def test_the_right_password_verifies(self) -> None:
        assert verify_password(hash_password(PASSWORD), PASSWORD) is True

    def test_the_wrong_password_does_not(self) -> None:
        assert verify_password(hash_password(PASSWORD), "something else entirely") is False

    def test_an_empty_password_does_not(self) -> None:
        assert verify_password(hash_password(PASSWORD), "") is False

    @pytest.mark.parametrize("stored", ["", "not-a-hash", "$argon2id$broken"])
    def test_a_corrupt_hash_fails_closed(self, stored: str) -> None:
        """A damaged row must reject everyone, never raise into the request."""
        assert verify_password(stored, PASSWORD) is False


class TestNeedsRehash:
    def test_a_fresh_hash_does_not(self) -> None:
        assert needs_rehash(hash_password(PASSWORD)) is False

    def test_a_corrupt_hash_is_not_reported_as_stale(self) -> None:
        assert needs_rehash("not-a-hash") is False


class TestTokens:
    def test_every_token_is_different(self) -> None:
        assert len({new_token() for _ in range(100)}) == 100

    def test_a_token_carries_the_expected_entropy(self) -> None:
        assert len(new_token()) >= TOKEN_BYTES

    def test_hashing_is_stable(self) -> None:
        token = new_token()

        assert hash_token(token) == hash_token(token)

    def test_the_hash_is_a_sha256_digest(self) -> None:
        """It is stored in a String(64) column, so the length matters."""
        digest = hash_token(new_token())

        assert len(digest) == SHA256_HEX_LENGTH
        assert int(digest, 16) >= 0

    def test_different_tokens_hash_differently(self) -> None:
        assert hash_token("one") != hash_token("two")

    def test_the_token_is_not_recoverable_from_the_hash(self) -> None:
        token = new_token()

        assert token not in hash_token(token)
