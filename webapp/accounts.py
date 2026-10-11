"""Accounts for the app outside Telegram (browser / home-screen app / store wrapper): e-mail + password login,
bearer tokens, account deletion, and who is an admin.

Admins are listed in .env (ADMIN_TELEGRAM_IDS, ADMIN_EMAILS), not in the database, so a database bug or a
compromised user row can never create an admin.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (AuthToken, ChallengeEquityPoint, ChallengeOrder, ChallengeParticipant, ExchangeAPIKey,
                             User)
from database.secure import hash_password, verify_password

TOKEN_DAYS = 30
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
_fails: dict[str, list[float]] = {}            # e-mail -> recent failure times (in memory: per process)


class AuthError(ValueError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_admin(user: User | None) -> bool:
    if user is None or not user.is_active:
        return False
    tg_ids = {x.strip() for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
    emails = {x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()}
    return (user.telegram_id is not None and str(user.telegram_id) in tg_ids) or \
        (bool(user.email) and user.email.lower() in emails and user.hashed_password not in ("!", ""))


async def issue_token(s: AsyncSession, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    s.add(AuthToken(user_id=user.id, token_hash=_hash(token), created_at=now,
                    expires_at=now + timedelta(days=TOKEN_DAYS)))
    await s.flush()
    return token


async def user_for_token(s: AsyncSession, token: str) -> User | None:
    if not token or len(token) > 200:
        return None
    row = (await s.execute(select(AuthToken).where(AuthToken.token_hash == _hash(token)))).scalar()
    if row is None:
        return None
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if exp <= datetime.now(timezone.utc):
        await s.delete(row)
        return None
    user = await s.get(User, row.user_id)
    return user if user is not None and user.is_active else None


async def revoke_token(s: AsyncSession, token: str) -> None:
    await s.execute(delete(AuthToken).where(AuthToken.token_hash == _hash(token)))


async def register(s: AsyncSession, email: str, password: str) -> User:
    email = (email or "").strip().lower()
    if len(password or "") > 200:
        raise AuthError("password too long")
    if email.endswith(".invalid"):
        raise AuthError("invalid e-mail address")
    if (await s.execute(select(User).where(User.email == email))).scalar():
        raise AuthError("an account with this e-mail already exists")
    try:
        user = User(email=email, hashed_password=hash_password(password),
                    subscription_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    except ValueError as exc:                    # bad e-mail or password shorter than 10 characters
        raise AuthError(str(exc)) from None
    user.display_name = email.split("@")[0][:3] + "***"
    s.add(user)
    await s.flush()
    return user


async def login(s: AsyncSession, email: str, password: str) -> User:
    email = (email or "").strip().lower()
    now = time.monotonic()
    recent = [t for t in _fails.get(email, []) if now - t < LOCK_SECONDS]
    if len(recent) >= MAX_FAILS:
        raise AuthError("too many failed attempts, try again in 15 minutes")
    user = (await s.execute(select(User).where(User.email == email))).scalar()
    # verify even when the user doesn't exist, so response time doesn't reveal which e-mails are registered
    ok = verify_password(password or "", user.hashed_password if user else "scrypt$16384$8$1$AAAA$AAAA")
    if not ok or user is None or not user.is_active:
        _fails[email] = recent + [now]
        raise AuthError("wrong e-mail or password")
    _fails.pop(email, None)
    return user


async def delete_account(s: AsyncSession, user: User) -> None:
    """Remove personal data and challenge history. Paid invoices stay (amount, tx id: needed for accounting) but
    no longer point to anything personal: the user row is anonymised and disabled."""
    pids = list((await s.execute(select(ChallengeParticipant.id)
                                 .where(ChallengeParticipant.user_id == user.id))).scalars())
    if pids:
        await s.execute(delete(ChallengeOrder).where(ChallengeOrder.participant_id.in_(pids)))
        await s.execute(delete(ChallengeEquityPoint).where(ChallengeEquityPoint.participant_id.in_(pids)))
        await s.execute(delete(ChallengeParticipant).where(ChallengeParticipant.id.in_(pids)))
    await s.execute(delete(AuthToken).where(AuthToken.user_id == user.id))
    await s.execute(delete(ExchangeAPIKey).where(ExchangeAPIKey.user_id == user.id))
    user.email = f"deleted{user.id}@deleted.invalid"
    user.hashed_password = "!"
    user.telegram_id = None
    user.display_name = None
    user.is_active = False
    await s.flush()
