"""Invoices, payment matching and the subscription guard.

    python -m billing adduser EMAIL            # asks for the password
    python -m billing invoice EMAIL [DAYS]     # prints the exact amount to pay and the address
    python -m billing check                    # match confirmed payments once (run every minute or two)
    python -m billing status EMAIL

.env: TRON_RECEIVE_ADDRESS (required), SUBSCRIPTION_PRICE_USDT (default 29), SUBSCRIPTION_DAYS (default 30),
INVOICE_TTL_HOURS (default 24), TRONGRID_API_KEY (optional, raises the rate limit).
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from billing.tron import Transfer
from database.models import Invoice, InvoiceStatus, User

UNITS = 1_000_000                    # USDT has 6 decimals on TRON
MAX_SUFFIX_CENTS = 99                # unique amounts: price + 0.01 … 0.99 USDT


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)   # SQLite returns naive UTC


def price_units() -> int:
    return int(Decimal(os.getenv("SUBSCRIPTION_PRICE_USDT", "29")) * UNITS)


async def create_invoice(s: AsyncSession, user: User, address: str, days: int | None = None,
                         ttl_hours: float | None = None, base_units: int | None = None) -> Invoice:
    """Open an invoice with an amount no other open invoice uses, so the payment can be attributed."""
    days = days or int(os.getenv("SUBSCRIPTION_DAYS", "30"))
    ttl = ttl_hours or float(os.getenv("INVOICE_TTL_HOURS", "24"))
    base = base_units if base_units is not None else price_units()
    await expire_invoices(s)
    now = datetime.now(timezone.utc)
    # an amount stays reserved for 3 days, so a late payment for an expired invoice can't credit someone else
    taken = set((await s.execute(select(Invoice.amount_units).where(
        (Invoice.status == InvoiceStatus.PENDING) | (Invoice.created_at >= now - timedelta(days=3))))).scalars())
    free = [base + c * UNITS // 100 for c in range(1, MAX_SUFFIX_CENTS + 1) if base + c * UNITS // 100 not in taken]
    if not free:
        raise RuntimeError("all 99 payment amounts are reserved (3-day window); try again later")
    inv = Invoice(user_id=user.id, amount_units=secrets.choice(free), address=address, days=days,
                  status=InvoiceStatus.PENDING, created_at=now, expires_at=now + timedelta(hours=ttl))
    s.add(inv)
    await s.flush()
    return inv


async def expire_invoices(s: AsyncSession, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    rows = (await s.execute(select(Invoice).where(Invoice.status == InvoiceStatus.PENDING))).scalars().all()
    n = 0
    for inv in rows:
        if _aware(inv.expires_at) <= now:
            inv.status = InvoiceStatus.EXPIRED
            n += 1
    return n


def extend_subscription(user: User, days: int, now: datetime | None = None) -> None:
    """Paying early adds to the remaining time instead of losing it."""
    now = now or datetime.now(timezone.utc)
    start = max(_aware(user.subscription_expires_at), now)
    user.subscription_expires_at = start + timedelta(days=days)


async def apply_transfers(s: AsyncSession, transfers: list[Transfer], address: str) -> list[Invoice]:
    """Mark matching invoices paid and extend subscriptions. Each transaction id is used at most once."""
    used = set((await s.execute(select(Invoice.tx_id).where(Invoice.tx_id.is_not(None)))).scalars())
    pending = (await s.execute(select(Invoice).where(Invoice.status == InvoiceStatus.PENDING,
                                                     Invoice.address == address))).scalars().all()
    paid: list[Invoice] = []
    for t in sorted(transfers, key=lambda t: t.at):
        if t.tx_id in used or t.to != address:
            continue
        match = next((inv for inv in pending if inv.status == InvoiceStatus.PENDING
                      and inv.amount_units == t.amount_units
                      and _aware(inv.created_at) <= t.at <= _aware(inv.expires_at)), None)
        if match is None:
            logger.warning("unmatched USDT payment {} of {} (wrong amount or late) — check manually",
                           t.tx_id, t.amount_units / UNITS)
            continue
        user = await s.get(User, match.user_id)
        match.status, match.tx_id, match.paid_at = InvoiceStatus.PAID, t.tx_id, t.at
        if user is not None:
            extend_subscription(user, match.days)
        used.add(t.tx_id)
        paid.append(match)
        logger.info("invoice {} paid by tx {} — user {} subscribed until {}", match.id, t.tx_id, match.user_id,
                    user.subscription_expires_at if user else "?")
    return paid


def subscription_allows_trading(user: User) -> bool:
    """Billing guard for a per-user engine: check before every cycle. An expired subscription only pauses
    trading; it does not disable the account (is_active is for bans), so paying again resumes it."""
    if not user.subscription_active:
        logger.warning("subscription inactive for user {} (expired {}), trading paused", user.id,
                       user.subscription_expires_at)
        return False
    return True
