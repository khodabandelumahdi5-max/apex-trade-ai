"""Database side of the signal service: scanning for new BUY signals, watching open ones, trader actions."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.risk_agent import take_profit_price
from config import Settings
from core.state import MarketDataState, TechnicalState
from database.models import SignalStatus, TradeSignal
from signalbot.core import (Alert, SignalConfig, _aware, e, evaluate_idea, evaluate_position, new_signal, pct, px,
                            signal_text, trend_alert)

ACTIVE = (SignalStatus.IDEA.value, SignalStatus.ENTERED.value)
TechFn = Callable[[str], Awaitable[tuple[TechnicalState, MarketDataState]]]
QuoteFn = Callable[[str], Awaitable[tuple[float, float]]]          # symbol -> (bid, ask)


class SignalError(ValueError):
    pass


async def active_signals(s: AsyncSession, symbol: str | None = None) -> list[TradeSignal]:
    q = select(TradeSignal).where(TradeSignal.status.in_(ACTIVE)).order_by(TradeSignal.id)
    if symbol:
        q = q.where(TradeSignal.symbol == symbol)
    return list((await s.execute(q)).scalars().all())


async def scan(s: AsyncSession, tech_fn: TechFn, settings: Settings, cfg: SignalConfig,
               now: datetime | None = None) -> tuple[list[TradeSignal], list[Alert], list[str]]:
    """New BUY signals, trend-exit alerts for open positions, and per-symbol errors."""
    now = now or datetime.now(timezone.utc)
    created: list[TradeSignal] = []
    alerts: list[Alert] = []
    errors: list[str] = []
    for sym in cfg.symbols:
        try:
            tech, market = await tech_fn(sym)
        except Exception as exc:                      # one symbol failing must not stop the others
            errors.append(f"{sym}: {exc}")
            continue
        active = await active_signals(s, sym)
        bid = market.bid or market.price
        for sig in active:
            alerts += trend_alert(sig, tech.signal, bid)
        if tech.signal != "BUY" or tech.confidence < cfg.min_confidence or active or not tech.suggested_stop:
            continue
        recent = (await s.execute(select(TradeSignal).where(
            TradeSignal.symbol == sym, TradeSignal.created_at >= now - timedelta(hours=cfg.cooldown_hours)))).scalar()
        if recent is not None:                        # one signal per symbol per cooldown: no spam on the same move
            continue
        ask = market.ask or market.price
        sig = new_signal(sym, ask, tech.suggested_stop, tech.confidence, tech.reasons, settings, cfg, now)
        if sig is None:
            logger.info("signal for {} skipped: stop distance or size not tradable", sym)
            continue
        s.add(sig)
        await s.flush()
        created.append(sig)
    return created, alerts, errors


async def watch(s: AsyncSession, quote_fn: QuoteFn, settings: Settings, cfg: SignalConfig,
                now: datetime | None = None) -> list[Alert]:
    now = now or datetime.now(timezone.utc)
    alerts: list[Alert] = []
    quotes: dict[str, tuple[float, float]] = {}
    for sig in await active_signals(s):
        if sig.symbol not in quotes:
            quotes[sig.symbol] = await quote_fn(sig.symbol)       # errors propagate: the caller tracks outages
        bid, ask = quotes[sig.symbol]
        if sig.status == SignalStatus.IDEA.value:
            alerts += evaluate_idea(sig, bid, ask, cfg, now)
        else:
            alerts += evaluate_position(sig, bid, settings, now)
    return alerts


async def get_signal(s: AsyncSession, raw_id: str | int) -> TradeSignal:
    try:
        sid = int(str(raw_id).lstrip("#"))
    except ValueError:
        raise SignalError("شماره‌ی سیگنال باید عدد باشد") from None
    sig = await s.get(TradeSignal, sid)
    if sig is None:
        raise SignalError(f"سیگنال #{sid} پیدا نشد")
    return sig


def enter(sig: TradeSignal, price: float, settings: Settings, cfg: SignalConfig, now: datetime | None = None) -> str:
    """The trader bought at `price`. Targets are recomputed from the real entry; the stop stays where it was."""
    now = now or datetime.now(timezone.utc)
    if sig.status != SignalStatus.IDEA.value:
        raise SignalError(f"سیگنال #{sig.id} در وضعیت {sig.status} است")
    if not price > sig.initial_stop:
        raise SignalError(f"قیمت ورود {px(price)} زیر حد ضرر {px(sig.initial_stop)} است: این معامله را نگیر")
    sig.status, sig.user_entry, sig.entered_at, sig.highest = SignalStatus.ENTERED.value, price, now, price
    sig.tp1 = take_profit_price(price, sig.initial_stop, settings.tp1_r)
    sig.tp2 = take_profit_price(price, sig.initial_stop, settings.tp2_r)
    warn = ""
    if price > sig.entry * (1 + cfg.max_chase_pct / 100):
        warn = f"\n⚠️ ورودت {pct(price, sig.entry)} بالاتر از قیمت سیگنال است؛ ریسک بیشتر و سود کمتر."
    return (f"✅ ورود #{sig.id} {e(sig.symbol)} در {px(price)} ثبت شد. از الان مراقبم.\n"
            f"حد ضرر: <b>{px(sig.stop)}</b> ({pct(sig.stop, price)}) ← <b>همین الان در صرافی ثبتش کن</b>\n"
            f"هدف ۱: {px(sig.tp1)} ({pct(sig.tp1, price) if sig.tp1 else '-'}) · هدف ۲: {px(sig.tp2)} "
            f"({pct(sig.tp2, price) if sig.tp2 else '-'}){warn}")


def close(sig: TradeSignal, price: float | None, reason: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if sig.status not in ACTIVE:
        raise SignalError(f"سیگنال #{sig.id} قبلاً بسته شده")
    was_entered = sig.status == SignalStatus.ENTERED.value
    sig.status = (SignalStatus.CLOSED if was_entered else SignalStatus.SKIPPED).value
    sig.close_reason, sig.exit_price, sig.closed_at = reason, price, now
    if was_entered and price:
        return f"📕 #{sig.id} {e(sig.symbol)} بسته شد در {px(price)} · نتیجه {pct(price, sig.user_entry or sig.entry)}"
    return f"⏭ سیگنال #{sig.id} {e(sig.symbol)} کنار گذاشته شد."


def list_text(signals: list[TradeSignal], prices: dict[str, float]) -> str:
    if not signals:
        return "سیگنال فعالی نیست. هر چند دقیقه بازار را بررسی می‌کنم و اگر موقعیتی بود خبرت می‌کنم."
    lines = ["<b>سیگنال‌های فعال</b>"]
    for sig in signals:
        now_px = prices.get(sig.symbol)
        if sig.status == SignalStatus.ENTERED.value:
            entry = sig.user_entry or sig.entry
            res = f" · الان {px(now_px)} ({pct(now_px, entry)})" if now_px else ""
            lines.append(f"• #{sig.id} {e(sig.symbol)} <b>داخل معامله</b> از {px(entry)} · حد ضرر {px(sig.stop)}{res}")
        else:
            age = (datetime.now(timezone.utc) - _aware(sig.created_at)).total_seconds() / 60
            lines.append(f"• #{sig.id} {e(sig.symbol)} ایده‌ی خرید {px(sig.entry)} · حد ضرر {px(sig.initial_stop)} "
                         f"· {age:.0f} دقیقه پیش")
    return "\n".join(lines)


def plan_text(rows: list[tuple[str, TechnicalState | None, MarketDataState | None, str | None]]) -> str:
    """Current state of every watched symbol (like signals.py)."""
    lines = ["<b>وضعیت فعلی بازار</b> (کندل‌های بسته‌شده‌ی ۱ و ۴ ساعته MEXC)"]
    for sym, tech, market, err in rows:
        if err or tech is None or market is None:
            lines.append(f"• {e(sym)}: داده در دسترس نیست ({e(err or '?')})")
            continue
        word = {"BUY": "🟢 خرید", "SELL": "🔴 روند نزولی (خرید نه)", "HOLD": "⚪ صبر"}[tech.signal]
        lines.append(f"• <b>{e(sym)}</b> {px(market.price)} · {word} · اطمینان {tech.confidence:.2f} · "
                     f"RSI {tech.rsi_14:.0f}")
    lines.append("سیگنال خرید فقط وقتی می‌آید که روند ۴ و ۱ ساعته صعودی و اطمینان ≥ آستانه باشد.")
    return "\n".join(lines)
