"""Verify Telegram Mini App initData (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).

The page sends Telegram.WebApp.initData; only Telegram (holder of the bot token) can produce a valid hash, so a
verified user id can be trusted. Never trust initDataUnsafe or a user id sent by the page itself.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    pass


def verify_init_data(init_data: str, bot_token: str, max_age_s: int = 86_400, now: float | None = None) -> dict:
    """Return the Telegram user dict, or raise InitDataError."""
    if not init_data or not bot_token:
        raise InitDataError("missing init data")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise InitDataError("malformed init data") from exc
    received = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("bad signature")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError) as exc:
        raise InitDataError("malformed init data") from exc
    if (now or time.time()) - auth_date > max_age_s:
        raise InitDataError("init data expired")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("no user")
    return user


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Test helper: build initData the way Telegram does."""
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})
