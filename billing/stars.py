"""Subscriptions paid with Telegram Stars (currency XTR).

Telegram's rules require Stars for digital goods and services sold inside bots and Mini Apps, so inside Telegram
this is the payment method; USDT (billing/service.py) stays for the web app outside Telegram.

Flow: invoice (bot.send_invoice / create_invoice_link, payload "sub:<user>:<days>:<stars>") → Telegram asks
pre_checkout → validate_checkout() → user pays → successful_payment → record_payment() (idempotent by charge id).
.env: STARS_PRICE (stars per period, 0/empty = off), SUBSCRIPTION_DAYS.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from billing.service import extend_subscription
from database.models import StarPayment, User

CURRENCY = "XTR"


def stars_price() -> int:
    try:
        return max(0, int(os.getenv("STARS_PRICE", "0") or 0))
    except ValueError:
        return 0


def period_days() -> int:
    return int(os.getenv("SUBSCRIPTION_DAYS", "30"))


def make_payload(user: User) -> str:
    return f"sub:{user.id}:{period_days()}:{stars_price()}"


def invoice_args(user: User) -> dict:
    """Keyword arguments for bot.send_invoice / bot.create_invoice_link."""
    from telegram import LabeledPrice
    days, stars = period_days(), stars_price()
    return {"title": f"AlphaDuel – {days} days", "description":
            f"Access to the AlphaDuel paper-trading discipline challenges for {days} days.",
            "payload": make_payload(user), "currency": CURRENCY, "prices": [LabeledPrice(f"{days} days", stars)]}


def _parse(payload: str) -> tuple[int, int, int] | None:
    parts = (payload or "").split(":")
    if len(parts) != 4 or parts[0] != "sub" or not all(p.isdigit() for p in parts[1:]):
        return None
    return int(parts[1]), int(parts[2]), int(parts[3])


async def validate_checkout(s: AsyncSession, payload: str, from_telegram_id: int, currency: str,
                            total: int) -> str | None:
    """None = OK to charge, otherwise the message Telegram shows to the user."""
    parsed = _parse(payload)
    if parsed is None or currency != CURRENCY:
        return "This invoice is not valid. Please request a new one."
    user_id, days, stars = parsed
    user = await s.get(User, user_id)
    if user is None or user.telegram_id != from_telegram_id:
        return "This invoice belongs to another account."
    if not user.is_active:
        return "This account is disabled."
    if total != stars or stars != stars_price() or days != period_days() or stars <= 0:
        return "The price has changed. Please request a new invoice."
    return None


async def record_payment(s: AsyncSession, payload: str, from_telegram_id: int, currency: str, total: int,
                         charge_id: str) -> tuple[User | None, bool]:
    """Extend the subscription once per charge id. Returns (user, newly_recorded)."""
    if (await s.execute(select(StarPayment).where(StarPayment.charge_id == charge_id))).scalar():
        return None, False
    parsed = _parse(payload)
    if parsed is None or currency != CURRENCY:
        raise ValueError(f"unexpected payment payload {payload!r} {currency}")
    user_id, days, _ = parsed
    user = await s.get(User, user_id)
    if user is None or user.telegram_id != from_telegram_id:
        raise ValueError(f"payment {charge_id} does not match user {user_id}")
    s.add(StarPayment(user_id=user.id, telegram_id=from_telegram_id, stars=total, days=days, charge_id=charge_id,
                      created_at=datetime.now(timezone.utc)))
    extend_subscription(user, days)
    await s.flush()
    return user, True


def apply_refund(user: User, payment: StarPayment) -> None:
    """After Telegram confirmed the refund: take the paid days back."""
    payment.refunded_at = datetime.now(timezone.utc)
    exp = user.subscription_expires_at
    exp = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
    user.subscription_expires_at = exp - timedelta(days=payment.days)
