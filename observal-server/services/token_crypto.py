"""Fernet-based at-rest encryption for OAuth tokens."""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from config import settings


class TokenCryptoError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    key = settings.OAUTH_TOKEN_ENCRYPTION_KEY
    if not key:
        raise TokenCryptoError(
            "OAUTH_TOKEN_ENCRYPTION_KEY is not configured. Generate one with "
            '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` '
            "and set it in the server environment."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise TokenCryptoError(f"OAUTH_TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {e}") from e


def encrypt(plaintext: str) -> bytes:
    if not plaintext:
        raise TokenCryptoError("Cannot encrypt an empty token")
    return _cipher().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    try:
        return _cipher().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as e:
        raise TokenCryptoError("Failed to decrypt OAuth token; wrong key or corrupted ciphertext") from e


def reset_cipher_cache() -> None:
    """Clear the memoised Fernet instance. Used by tests that rotate the key."""
    _cipher.cache_clear()
