"""Bot logic without Telegram objects (testable). Every function returns HTML-safe text for parse_mode=HTML.

All writes in the bot process go through LOCK: Telegram handlers and the background jobs run concurrently on one
event loop and would otherwise overwrite each other's changes to the same participant row.
"""
from __future__ import annotations

import asyncio
import html
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from billing.service import create_invoice
from competition import engine
from competition.engine import ChallengeError, Event, Quote, RuleViolation
from database.models import Challenge, ChallengeParticipant, Invoice, InvoiceStatus, User

LOCK = asyncio.Lock()
TG_EMAIL_DOMAIN = "telegram.invalid"           # reserved TLD: can never be a real mailbox
e = html.escape


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_price(x: float) -> str:
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}" if x >= 1 else f"{x:.6f}"


async def get_or_create_user(s: AsyncSession, telegram_id: int, first_name: str | None) -> User:
    user = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar()
    if user is None:
        user = User(email=f"tg{telegram_id}@{TG_EMAIL_DOMAIN}", hashed_password="!", telegram_id=telegram_id,
                    subscription_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        s.add(user)
    name = (first_name or "").strip()[:24]
    if name and user.display_name != name:
        user.display_name = name
    await s.flush()
    return user


def subscription_text(user: User) -> str:
    exp = _aware(user.subscription_expires_at)
    if user.subscription_active:
        days = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
        return f"✅ Subscription active until <b>{exp:%Y-%m-%d %H:%M} UTC</b> ({days:.1f} days left)."
    return "❌ No active subscription. Tap <b>Subscribe</b> to get a USDT (TRC20) invoice."


async def invoice_text(s: AsyncSession, user: User) -> str:
    address = os.getenv("TRON_RECEIVE_ADDRESS", "").strip()
    if not address:
        return "Payments are not configured yet. Please try again later."
    now = datetime.now(timezone.utc)
    inv = (await s.execute(select(Invoice).where(Invoice.user_id == user.id,
                                                 Invoice.status == InvoiceStatus.PENDING)
                           .order_by(Invoice.created_at.desc()))).scalars().first()
    if inv is None or _aware(inv.expires_at) <= now + timedelta(minutes=30):
        inv = await create_invoice(s, user, address)          # one open invoice per user; no spam invoices
    left = (_aware(inv.expires_at) - now).total_seconds() / 3600
    return (f"💳 <b>Subscription: {inv.days} days</b>\n\n"
            f"Send <b>exactly</b> <code>{e(inv.amount_usdt)}</code> USDT\n"
            f"Network: <b>TRON (TRC20)</b> only\n"
            f"To: <code>{e(address)}</code>\n\n"
            f"⏳ Valid for {left:.0f} h. The exact amount identifies your payment: a different amount, another "
            f"token or another network cannot be matched automatically. You get a message here after the "
            f"transfer is confirmed (usually 1–3 minutes).\n\n"
            f"Paper-trading challenge platform. Not financial advice. No refunds for wrong network or amount.")


async def live_challenges(s: AsyncSession) -> list[Challenge]:
    now = datetime.now(timezone.utc)
    rows = (await s.execute(select(Challenge).where(Challenge.finalized.is_(False))
                            .order_by(Challenge.start_time))).scalars().all()
    return [c for c in rows if _aware(c.end_time) > now]


def challenge_text(ch: Challenge) -> str:
    rules = [f"start balance ${ch.starting_balance:,.0f} (virtual)", f"symbols {', '.join(ch.symbol_list)}"]
    if ch.max_risk_per_trade_pct:
        rules.append(f"max risk per trade {ch.max_risk_per_trade_pct:g}% (stop-loss required)")
    if ch.daily_loss_limit_pct:
        rules.append(f"daily loss over {ch.daily_loss_limit_pct:g}% = disqualified")
    if ch.max_drawdown_pct:
        rules.append(f"drawdown over {ch.max_drawdown_pct:g}% = disqualified")
    if ch.max_consecutive_losses:
        rules.append(f"{ch.max_consecutive_losses} losses in a row = {ch.cooldown_hours:g} h pause")
    if ch.max_entries_per_day:
        rules.append(f"max {ch.max_entries_per_day} entries per day")
    return (f"🏆 <b>#{ch.id} {e(ch.title)}</b>\n"
            f"Ends {_aware(ch.end_time):%Y-%m-%d %H:%M} UTC · prize: {e(ch.prize_text or '-')}\n"
            + "\n".join(f"• {e(r)}" for r in rules))


async def load_challenge(s: AsyncSession, raw_id: str) -> Challenge:
    try:
        cid = int(str(raw_id).lstrip("#"))
    except ValueError:
        raise ChallengeError("challenge id must be a number") from None
    ch = await s.get(Challenge, cid)
    if ch is None:
        raise ChallengeError(f"no challenge #{cid}")
    return ch


def participant_text(ch: Challenge, p: ChallengeParticipant) -> str:
    lines = [f"📊 <b>#{ch.id} {e(ch.title)}</b> — {p.status}"
             + (f" ({e(p.status_reason)})" if p.status_reason else ""),
             f"Rank: <b>{p.rank or '-'}</b> · ROI <b>{p.roi_pct:+.2f}%</b> · equity ${p.equity:,.2f}",
             f"Discipline {engine.discipline_score(p):.0f}/100 · max DD {p.max_drawdown_pct:.2f}% · "
             f"W/L {p.wins}/{p.losses} · cash ${p.cash:,.2f}"]
    if p.cooldown_until and _aware(p.cooldown_until) > datetime.now(timezone.utc):
        lines.append(f"⏸ Cooldown until {_aware(p.cooldown_until):%Y-%m-%d %H:%M} UTC")
    for sym, h in (p.holdings or {}).items():
        lines.append(f"• {e(sym)}: {h['qty']:.6f} · avg {fmt_price(h['cost'] / h['qty'])} · "
                     f"stop {fmt_price(h['stop'])}")
    return "\n".join(lines)


def board_text(ch: Challenge, rows: list[ChallengeParticipant], names: dict[int, str], limit: int = 10) -> str:
    lines = [f"🏁 <b>#{ch.id} {e(ch.title)}</b> {'(FINAL)' if ch.finalized else '(live)'}"]
    ranked = [p for p in rows if p.rank]
    if not ranked:
        lines.append("No ranked traders yet.")
    for p in ranked[:limit]:
        lines.append(f"{p.rank}. {e(names.get(p.user_id, '?'))} — ROI {p.roi_pct:+.2f}% · "
                     f"discipline {engine.discipline_score(p):.0f} · DD {p.max_drawdown_pct:.1f}%")
    return "\n".join(lines)


def _num(raw: str, what: str) -> float:
    try:
        v = float(raw.replace(",", ""))
    except ValueError:
        raise ChallengeError(f"{what} must be a number") from None
    if not v > 0 or v != v or v == float("inf"):
        raise ChallengeError(f"{what} must be positive")
    return v


async def trade_command(s: AsyncSession, user: User, cmd: str, args: list[str], quotes) -> str:
    """/buy ID SYMBOL USD STOP · /sell ID SYMBOL [QTY] · /stop ID SYMBOL PRICE. `quotes(symbol)` → Quote."""
    usage = {"buy": "/buy ID SYMBOL USD STOP   e.g. /buy 1 SOL 500 140",
             "sell": "/sell ID SYMBOL [QTY]   e.g. /sell 1 SOL",
             "stop": "/stop ID SYMBOL NEW_STOP   e.g. /stop 1 SOL 150"}[cmd]
    need = {"buy": 4, "sell": 2, "stop": 3}[cmd]
    if len(args) < need or len(args) > need + (1 if cmd == "sell" else 0):
        return f"Usage: <code>{e(usage)}</code>"
    try:
        ch = await load_challenge(s, args[0])
        p = await engine.get_participant(s, ch, user)
        sym = args[1].upper()
        if sym not in ch.symbol_list:
            raise ChallengeError(f"{sym} is not traded in this challenge")
        q: Quote = await quotes(sym)
        if cmd == "buy":
            o = await engine.buy(s, ch, p, sym, q, usd=_num(args[2], "USD"), stop=_num(args[3], "stop"))
            h = p.holdings[sym]
            risk = engine.stop_loss_risk(h["qty"], h["cost"], h["stop"], ch.fee_bps / 10_000)
            return (f"✅ Bought {o.qty:.6f} {e(sym)} @ {fmt_price(o.price)} (fee ${o.fee_usd:.2f})\n"
                    f"Stop {fmt_price(h['stop'])} · risk ${risk:,.2f} · cash ${p.cash:,.2f}")
        if cmd == "sell":
            o, events = await engine.sell(s, ch, p, sym, q, qty=_num(args[2], "QTY") if len(args) > 2 else None)
            extra = "".join(f"\n{event_text(ev)}" for ev in events)
            return (f"✅ Sold {o.qty:.6f} {e(sym)} @ {fmt_price(o.price)} · P&L ${o.pnl:+,.2f} · "
                    f"cash ${p.cash:,.2f}{extra}")
        await engine.set_stop(ch, p, sym, _num(args[2], "stop"), q)
        return f"✅ {e(sym)} stop raised to {fmt_price(p.holdings[sym]['stop'])}"
    except RuleViolation as exc:             # caught here, inside the session, so the rule break is saved
        return f"⛔ Rule: {e(str(exc))}"
    except ChallengeError as exc:
        return f"⚠️ {e(str(exc))}"


def event_text(ev: Event) -> str:
    if ev.kind == "stop_hit":
        return (f"🛑 #{ev.challenge_id} stop hit: {e(ev.symbol or '')} sold @ {fmt_price(ev.price or 0)} "
                f"({e(ev.detail)}) · P&L ${ev.pnl or 0:+,.2f}")
    if ev.kind == "near_stop":
        return (f"⚠️ #{ev.challenge_id} {e(ev.symbol or '')} at {fmt_price(ev.price or 0)} is within "
                f"{engine.NEAR_STOP_PCT:g}% of its {e(ev.detail)}")
    if ev.kind == "cooldown":
        return f"⏸ #{ev.challenge_id} {e(ev.detail)}"
    if ev.kind == "disqualified":
        return f"❌ #{ev.challenge_id} disqualified: {e(ev.detail)}. Positions were closed."
    if ev.kind == "completed":
        return f"🏁 #{ev.challenge_id} finished — {e(ev.detail)}. Well done for staying disciplined!"
    return e(f"{ev.kind}: {ev.detail}")
