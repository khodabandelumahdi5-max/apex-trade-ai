from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from database import secure
from database.connection import dispose_engine, get_session, init_db
from database.models import ExchangeAPIKey, User


@pytest.fixture
def key(monkeypatch):
    k = secure.generate_key()
    monkeypatch.setenv(secure.ENV_VAR, k)
    secure.reset_cipher_cache()
    yield k
    secure.reset_cipher_cache()


@pytest_asyncio.fixture
async def db(tmp_path, key):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/secure.db")
    yield
    await dispose_engine()


def _user(email: str = "Trader@Example.com ") -> User:
    return User(email=email, hashed_password=secure.hash_password("correct horse battery"),
                subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30))


def test_missing_key_is_an_error_not_a_random_key(monkeypatch):
    monkeypatch.delenv(secure.ENV_VAR, raising=False)
    secure.reset_cipher_cache()
    with pytest.raises(secure.EncryptionKeyError):
        secure.encrypt("x")


def test_malformed_key(monkeypatch):
    monkeypatch.setenv(secure.ENV_VAR, "not-a-key")
    secure.reset_cipher_cache()
    with pytest.raises(secure.EncryptionKeyError):
        secure.encrypt("x")
    secure.reset_cipher_cache()


def test_roundtrip_and_wrong_key(key, monkeypatch):
    token = secure.encrypt("my-secret")
    assert token != "my-secret" and secure.decrypt(token) == "my-secret"
    monkeypatch.setenv(secure.ENV_VAR, secure.generate_key())
    secure.reset_cipher_cache()
    with pytest.raises(secure.DecryptionError):
        secure.decrypt(token)


def test_rotation_old_key_still_decrypts(key, monkeypatch):
    old_token = secure.encrypt("s")
    new = secure.generate_key()
    monkeypatch.setenv(secure.ENV_VAR, f"{new},{key}")
    secure.reset_cipher_cache()
    assert secure.decrypt(old_token) == "s"
    fresh = secure.encrypt("s")
    monkeypatch.setenv(secure.ENV_VAR, new)            # old key removed after rotation
    secure.reset_cipher_cache()
    assert secure.decrypt(fresh) == "s"


def test_password_hashing():
    h = secure.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert secure.verify_password("correct horse battery", h)
    assert not secure.verify_password("wrong password!!", h)
    assert not secure.verify_password("x", "garbage")
    assert h != secure.hash_password("correct horse battery")      # random salt
    with pytest.raises(ValueError):
        secure.hash_password("short")


def test_model_validators(key):
    with pytest.raises(ValueError):
        ExchangeAPIKey(exchange_name="mexc", api_key="k", api_secret="s", can_withdraw=True)
    with pytest.raises(ValueError):
        ExchangeAPIKey(exchange_name="ftx", api_key="k", api_secret="s")
    with pytest.raises(ValueError):
        User(email="no-at-sign", hashed_password="x", subscription_expires_at=datetime.now(timezone.utc))
    row = ExchangeAPIKey(exchange_name=" MEXC ", api_key="AKEY123", api_secret="SSECRET456")
    assert row.exchange_name == "mexc"
    assert "AKEY123" not in repr(row) and "SSECRET456" not in repr(row)


def test_subscription_active():
    u = _user()
    u.is_active = True
    assert u.subscription_active
    u.subscription_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not u.subscription_active
    u.subscription_expires_at = datetime.utcnow() + timedelta(days=1)    # naive (SQLite) is read as UTC
    assert u.subscription_active
    u.is_active = False
    assert not u.subscription_active


@pytest.mark.asyncio
async def test_secrets_are_encrypted_on_disk(db):
    async with get_session() as s:
        u = _user()
        u.api_keys.append(ExchangeAPIKey(exchange_name="mexc", api_key="AKEY123", api_secret="SSECRET456"))
        s.add(u)
    async with get_session() as s:
        raw = (await s.execute(text("SELECT api_key, api_secret FROM exchange_api_keys"))).one()
        assert "AKEY123" not in raw[0] and "SSECRET456" not in raw[1]
        row = (await s.execute(select(ExchangeAPIKey))).scalar_one()
        assert (row.api_key, row.api_secret) == ("AKEY123", "SSECRET456")
        user = (await s.execute(select(User))).scalar_one()
        assert user.email == "trader@example.com"


@pytest.mark.asyncio
async def test_rotate_reencrypts_with_new_primary(db, key, monkeypatch, tmp_path):
    async with get_session() as s:
        u = _user()
        u.api_keys.append(ExchangeAPIKey(exchange_name="mexc", api_key="AKEY123", api_secret="SSECRET456"))
        s.add(u)
    new = secure.generate_key()
    monkeypatch.setenv(secure.ENV_VAR, f"{new},{key}")
    secure.reset_cipher_cache()
    from sqlalchemy.orm.attributes import flag_modified
    async with get_session() as s:                      # same steps as `python -m database.secure rotate`
        for row in (await s.execute(select(ExchangeAPIKey))).scalars():
            flag_modified(row, "api_key")
            flag_modified(row, "api_secret")
    monkeypatch.setenv(secure.ENV_VAR, new)
    secure.reset_cipher_cache()
    async with get_session() as s:
        row = (await s.execute(select(ExchangeAPIKey))).scalar_one()
        assert row.api_secret == "SSECRET456"
