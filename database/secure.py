"""Encryption at rest for exchange API secrets, and password hashing.

DB_ENCRYPTION_KEY (in .env) holds one or more Fernet keys, comma-separated. The first key encrypts; all keys can
decrypt, so a leaked key can be rotated: put the new key first, keep the old one second, run
`python -m database.secure rotate`, then remove the old key.

Generate a key:  python -m database.secure genkey

The key is REQUIRED for these columns. Earlier drafts fell back to Fernet.generate_key() when the variable was
missing, which silently creates a new random key on every start and makes every stored secret unreadable after the
first restart.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

ENV_VAR = "DB_ENCRYPTION_KEY"


class EncryptionKeyError(RuntimeError):
    """DB_ENCRYPTION_KEY missing or malformed."""


class DecryptionError(RuntimeError):
    """A stored value cannot be decrypted with any configured key."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


@lru_cache(maxsize=1)
def _cipher() -> MultiFernet:
    raw = os.getenv(ENV_VAR, "").strip()
    if not raw:
        raise EncryptionKeyError(
            f"{ENV_VAR} is not set. Generate one with `python -m database.secure genkey`, put it in .env and keep a "
            "backup: without it, stored API keys cannot be decrypted.")
    try:
        return MultiFernet([Fernet(k.strip().encode()) for k in raw.split(",") if k.strip()])
    except (ValueError, TypeError) as exc:
        raise EncryptionKeyError(f"{ENV_VAR} is not a valid Fernet key: {exc}") from exc


def reset_cipher_cache() -> None:
    """For tests / after changing the environment variable."""
    _cipher.cache_clear()


def encrypt(plain: str) -> str:
    return _cipher().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(f"stored secret cannot be decrypted with the configured {ENV_VAR} "
                              "(wrong or rotated-out key)") from exc


class EncryptedString(TypeDecorator[str]):
    """Text column transparently encrypted with Fernet (AES-128-CBC + HMAC-SHA256)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        return None if value is None else encrypt(value)

    def process_result_value(self, value: str | None, dialect: object) -> str | None:
        return None if value is None else decrypt(value)


# --------------------------------------------------------------------------------------------- passwords
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


def hash_password(password: str) -> str:
    """scrypt with a random 16-byte salt; format: scrypt$N$r$p$salt$hash (base64)."""
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------------------------- CLI
async def _rotate() -> int:
    """Re-encrypt every stored secret with the first (primary) key."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from config import get_settings
    from database.connection import dispose_engine, get_session, init_db
    from database.models import ExchangeAPIKey

    await init_db(get_settings().database_url)
    count = 0
    try:
        async with get_session() as s:
            for row in (await s.execute(select(ExchangeAPIKey))).scalars():
                # unchanged values are not re-written unless flagged; on flush they are encrypted with the primary key
                flag_modified(row, "api_key")
                flag_modified(row, "api_secret")
                count += 1
    finally:
        await dispose_engine()
    print(f"re-encrypted {count} API key record(s) with the primary key")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["genkey"]:
        print(generate_key())
        return 0
    if argv[:1] == ["rotate"]:
        import asyncio
        from dotenv import load_dotenv
        load_dotenv()
        return asyncio.run(_rotate())
    print("usage: python -m database.secure genkey | rotate")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
