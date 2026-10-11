"""The 21-day discipline challenge. Prices come in as arguments, so every rule is deterministic and testable.

Trading
* BUY fills at the ask, SELL at the bid, plus the challenge fee: spread and fees are part of the game.
* Every BUY needs a stop-loss at least 0.05 % below the ask. The loss if that stop is hit (cost incl. entry fee
  minus stop proceeds after exit fee) must fit in max_risk_per_trade_pct of the participant's equity.
* Stops can only be raised (tightened). Stops are executed by `tick` at the bid, so a fast market can fill below
  the stop, as on a real exchange.

Rules (0 switches a rule off)
* Refused for a rule (no stop, risk too large, too many entries today, entry during cooldown, lowering a stop)
  → the order is not placed and rule_breaks += 1. Discipline score = 100 − 10 × rule_breaks (min 0).
* After max_consecutive_losses losing round trips in a row: no new entries for cooldown_hours (selling is allowed).
* Disqualified (positions closed at the bid, account frozen) when equity falls MORE than daily_loss_limit_pct below
  the day's first valuation (UTC day), or the drawdown from the peak exceeds max_drawdown_pct. Checked on every
  tick, so run `tick` often (the Telegram bot does it every minute).

Ranking: not disqualified and at least one trade → discipline score, then ROI, then lower max drawdown, then who
joined first. The first tick after the end time is final.

IMPORTANT for callers: RuleViolation is raised AFTER rule_breaks was incremented. Catch it inside the database
session (get_session rolls back on exceptions), otherwise the rule break is not recorded.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (Challenge, ChallengeEquityPoint, ChallengeOrder, ChallengeParticipant,
                             ChallengeStatus, User)

MIN_ORDER_USD = 1.0
MIN_STOP_DISTANCE = 0.0005           # 0.05 %: closer stops are noise and would make the size explode
NEAR_STOP_PCT = 1.0                  # warn once when the bid is within 1 % of the stop
POINT_EVERY = timedelta(minutes=15)
ACTIVE, DISQUALIFIED, COMPLETED = (ChallengeStatus.ACTIVE.value, ChallengeStatus.DISQUALIFIED.value,
                                   ChallengeStatus.COMPLETED.value)


class ChallengeError(ValueError):
    """Order refused for a non-rule reason (not enough cash, unknown symbol, challenge not running...)."""


class RuleViolation(ChallengeError):
    """Order refused because it breaks a discipline rule; counted in rule_breaks."""


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float


@dataclass
class Event:
    kind: str                         # stop_hit | near_stop | cooldown | disqualified | completed
    participant_id: int
    user_id: int
    challenge_id: int
    symbol: str | None = None
    price: float | None = None
    pnl: float | None = None
    detail: str = ""
    extra: dict = field(default_factory=dict)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def discipline_score(p: ChallengeParticipant) -> float:
    return max(0.0, 100.0 - 10.0 * p.rule_breaks)


def stop_loss_risk(qty: float, cost: float, stop: float, fee_rate: float) -> float:
    """USD lost if the whole position is sold exactly at the stop (cost already contains the entry fee)."""
    return cost - qty * stop * (1 - fee_rate)


def max_buy_usd(ch: Challenge, p: ChallengeParticipant, symbol: str, quote: Quote, stop: float) -> float:
    """Largest BUY (USD incl. fee) that keeps the whole position's stop-loss risk within the per-trade limit,
    also capped by cash. 0 when nothing fits (e.g. the existing position already uses the budget)."""
    if stop <= 0 or stop >= quote.ask:
        return 0.0
    f = ch.fee_bps / 10_000
    h = (p.holdings or {}).get(symbol.upper(), {"qty": 0.0, "cost": 0.0})
    cash = max(0.0, p.cash)
    if not ch.max_risk_per_trade_pct:
        return cash
    budget = p.equity * ch.max_risk_per_trade_pct / 100 - stop_loss_risk(h["qty"], h["cost"], stop, f)
    risk_per_usd = 1 - (1 - f) ** 2 * stop / quote.ask          # loss per $ spent if the stop is hit
    if budget <= 0:
        return 0.0
    return min(cash, budget / risk_per_usd * (1 - 1e-9))


def _check_quote(q: Quote) -> None:
    if not (q.bid > 0 and q.ask > 0 and q.bid <= q.ask):
        raise ChallengeError("invalid quote")


def _check_running(ch: Challenge, p: ChallengeParticipant, now: datetime) -> None:
    if p.status != ACTIVE:
        raise ChallengeError(f"you are {p.status.lower()}: {p.status_reason or ''}".strip())
    if not _aware(ch.start_time) <= now < _aware(ch.end_time) or ch.finalized:
        raise ChallengeError("challenge is not running")


def _rollover_day(p: ChallengeParticipant, now: datetime) -> None:
    key = now.astimezone(timezone.utc).date().isoformat()
    if p.day_key != key:
        p.day_key, p.day_start_equity, p.entries_today = key, p.equity, 0


def _violate(p: ChallengeParticipant, why: str) -> RuleViolation:
    p.rule_breaks += 1
    return RuleViolation(f"{why} (rule break #{p.rule_breaks}, discipline {discipline_score(p):.0f}/100)")


async def join(s: AsyncSession, ch: Challenge, user: User, now: datetime | None = None) -> ChallengeParticipant:
    now = now or _now()
    if now >= _aware(ch.end_time) or ch.finalized:
        raise ChallengeError("challenge has ended")
    if not user.subscription_active:
        raise ChallengeError("an active subscription is required to join")
    existing = (await s.execute(select(ChallengeParticipant).where(
        ChallengeParticipant.challenge_id == ch.id, ChallengeParticipant.user_id == user.id))).scalar()
    if existing:
        return existing
    p = ChallengeParticipant(challenge_id=ch.id, user_id=user.id, cash=ch.starting_balance, holdings={},
                             equity=ch.starting_balance, peak_equity=ch.starting_balance, joined_at=now,
                             status=ACTIVE, entries_today=0, consecutive_losses=0, rule_breaks=0,
                             realized_pnl=0.0, wins=0, losses=0, trades_count=0, max_drawdown_pct=0.0, roi_pct=0.0)
    _rollover_day(p, now)
    s.add(p)
    await s.flush()
    return p


async def get_participant(s: AsyncSession, ch: Challenge, user: User) -> ChallengeParticipant:
    p = (await s.execute(select(ChallengeParticipant).where(
        ChallengeParticipant.challenge_id == ch.id, ChallengeParticipant.user_id == user.id))).scalar()
    if p is None:
        raise ChallengeError("join the challenge first")
    return p


async def buy(s: AsyncSession, ch: Challenge, p: ChallengeParticipant, symbol: str, quote: Quote, *, usd: float,
              stop: float | None, now: datetime | None = None) -> ChallengeOrder:
    """Spend `usd` (fee included) at the ask with a mandatory stop-loss."""
    now = now or _now()
    symbol = symbol.upper()
    _check_running(ch, p, now)
    if symbol not in ch.symbol_list:
        raise ChallengeError(f"{symbol} is not traded in this challenge ({', '.join(ch.symbol_list)})")
    _check_quote(quote)
    _rollover_day(p, now)
    if not usd or usd < MIN_ORDER_USD:
        raise ChallengeError(f"minimum order is ${MIN_ORDER_USD:.0f}")
    if usd > p.cash + 1e-9:
        raise ChallengeError(f"not enough cash (${p.cash:,.2f})")
    if p.cooldown_until and now < _aware(p.cooldown_until):
        raise _violate(p, f"cooldown after {ch.max_consecutive_losses} losses in a row until "
                          f"{_aware(p.cooldown_until):%Y-%m-%d %H:%M} UTC")
    if ch.max_entries_per_day and p.entries_today >= ch.max_entries_per_day:
        raise _violate(p, f"overtrading: max {ch.max_entries_per_day} entries per day")
    if stop is None or stop <= 0:
        raise _violate(p, "a stop-loss is required for every buy")
    if stop >= quote.ask * (1 - MIN_STOP_DISTANCE):
        raise _violate(p, f"stop must be at least {MIN_STOP_DISTANCE:.2%} below the price {quote.ask}")

    fee_rate = ch.fee_bps / 10_000
    fee = usd * fee_rate
    qty = (usd - fee) / quote.ask
    holdings = copy.deepcopy(p.holdings or {})
    h = holdings.get(symbol, {"qty": 0.0, "cost": 0.0, "trip_pnl": 0.0})
    new_qty, new_cost = h["qty"] + qty, h["cost"] + usd
    risk = stop_loss_risk(new_qty, new_cost, stop, fee_rate)          # whole position, new stop
    budget = p.equity * ch.max_risk_per_trade_pct / 100
    if ch.max_risk_per_trade_pct and risk > budget + 1e-9:
        max_usd = max_buy_usd(ch, p, symbol, quote, stop)
        raise _violate(p, f"risk ${risk:,.2f} > {ch.max_risk_per_trade_pct}% of equity (${budget:,.2f}); "
                          f"with this stop buy at most ≈${max_usd:,.0f}")

    p.cash -= usd
    holdings[symbol] = {"qty": new_qty, "cost": new_cost, "stop": stop, "warned": False,
                        "trip_pnl": h.get("trip_pnl", 0.0)}
    p.holdings = holdings
    p.entries_today += 1
    p.trades_count += 1
    order = ChallengeOrder(participant_id=p.id, symbol=symbol, side="BUY", qty=qty, price=quote.ask, fee_usd=fee,
                           reason="manual", created_at=now)
    s.add(order)
    return order


def _sell(s: AsyncSession, ch: Challenge, p: ChallengeParticipant, symbol: str, qty: float, price: float,
          reason: str, now: datetime) -> tuple[ChallengeOrder, list[Event]]:
    """Execute a sell without permission checks (used by manual sells, stops and disqualification)."""
    fee_rate = ch.fee_bps / 10_000
    holdings = copy.deepcopy(p.holdings or {})
    h = holdings[symbol]
    qty = min(qty, h["qty"])
    full = qty >= h["qty"] * (1 - 1e-12)
    fraction = 1.0 if full else qty / h["qty"]
    cost_part = h["cost"] * fraction
    gross = qty * price
    fee = gross * fee_rate
    pnl = gross - fee - cost_part
    p.cash += gross - fee
    p.realized_pnl += pnl
    p.trades_count += 1
    events: list[Event] = []
    if full:
        trip = h.get("trip_pnl", 0.0) + pnl
        del holdings[symbol]
        if trip < 0:
            p.losses += 1
            p.consecutive_losses += 1
            if ch.max_consecutive_losses and p.consecutive_losses >= ch.max_consecutive_losses:
                p.cooldown_until = now + timedelta(hours=ch.cooldown_hours)
                p.consecutive_losses = 0
                events.append(Event("cooldown", p.id, p.user_id, ch.id,
                                    detail=f"{ch.max_consecutive_losses} losses in a row: no new entries for "
                                           f"{ch.cooldown_hours:g} h"))
        else:
            p.wins += 1
            p.consecutive_losses = 0
    else:
        h["qty"] -= qty
        h["cost"] -= cost_part
        h["trip_pnl"] = h.get("trip_pnl", 0.0) + pnl
    p.holdings = holdings
    order = ChallengeOrder(participant_id=p.id, symbol=symbol, side="SELL", qty=qty, price=price, fee_usd=fee,
                           reason=reason, pnl=pnl, created_at=now)
    s.add(order)
    return order, events


async def sell(s: AsyncSession, ch: Challenge, p: ChallengeParticipant, symbol: str, quote: Quote, *,
               qty: float | None = None, now: datetime | None = None) -> tuple[ChallengeOrder, list[Event]]:
    """Sell `qty` at the bid (None = the whole position). Allowed during a cooldown."""
    now = now or _now()
    symbol = symbol.upper()
    _check_running(ch, p, now)
    _check_quote(quote)
    held = (p.holdings or {}).get(symbol, {}).get("qty", 0.0)
    qty = held if qty is None else qty
    if held <= 0 or qty <= 0 or qty > held * (1 + 1e-12):
        raise ChallengeError(f"cannot sell {qty} {symbol}, holding {held}")
    if qty < held and qty * quote.bid < MIN_ORDER_USD:
        raise ChallengeError(f"minimum order is ${MIN_ORDER_USD:.0f}")
    return _sell(s, ch, p, symbol, qty, quote.bid, "manual", now)


async def set_stop(ch: Challenge, p: ChallengeParticipant, symbol: str, stop: float, quote: Quote,
                   now: datetime | None = None) -> None:
    """Raise the stop of an open position (e.g. to breakeven). Lowering it is a rule break."""
    now = now or _now()
    symbol = symbol.upper()
    _check_running(ch, p, now)
    _check_quote(quote)
    holdings = copy.deepcopy(p.holdings or {})
    if symbol not in holdings:
        raise ChallengeError(f"no open {symbol} position")
    if stop < holdings[symbol]["stop"]:
        raise _violate(p, "a stop can only be raised, never lowered")
    if stop >= quote.bid * (1 - MIN_STOP_DISTANCE):
        raise ChallengeError(f"stop must be below the current price {quote.bid}; to exit now, sell")
    holdings[symbol]["stop"] = stop
    holdings[symbol]["warned"] = False
    p.holdings = holdings


def mark_to_market(p: ChallengeParticipant, ch: Challenge, quotes: dict[str, Quote]) -> None:
    """Value holdings at the bid (what they could be sold for) and update ROI / peak / drawdown."""
    value = p.cash
    for sym, h in (p.holdings or {}).items():
        if sym not in quotes:
            raise ChallengeError(f"no quote for {sym}")
        value += h["qty"] * quotes[sym].bid
    p.equity = value
    p.peak_equity = max(p.peak_equity, value)
    if p.peak_equity > 0:
        p.max_drawdown_pct = max(p.max_drawdown_pct, (1 - value / p.peak_equity) * 100)
    p.roi_pct = (value / ch.starting_balance - 1) * 100


def _disqualify(s: AsyncSession, ch: Challenge, p: ChallengeParticipant, quotes: dict[str, Quote], reason: str,
                now: datetime) -> Event:
    for sym in list((p.holdings or {}).keys()):
        _sell(s, ch, p, sym, p.holdings[sym]["qty"], quotes[sym].bid, "disqualified", now)
    mark_to_market(p, ch, quotes)
    p.status, p.status_reason = DISQUALIFIED, reason
    return Event("disqualified", p.id, p.user_id, ch.id, detail=reason)


def rank(rows: list[ChallengeParticipant]) -> list[ChallengeParticipant]:
    eligible = sorted((p for p in rows if p.status != DISQUALIFIED and p.trades_count > 0),
                      key=lambda p: (-discipline_score(p), -round(p.roi_pct, 6), round(p.max_drawdown_pct, 6),
                                     _aware(p.joined_at), p.id))
    for i, p in enumerate(eligible, start=1):
        p.rank = i
    rest = [p for p in rows if p not in eligible]
    for p in rest:
        p.rank = None
    rest.sort(key=lambda p: (p.status == DISQUALIFIED, -p.roi_pct))
    return eligible + rest


async def participants(s: AsyncSession, ch: Challenge) -> list[ChallengeParticipant]:
    return list((await s.execute(select(ChallengeParticipant)
                                 .where(ChallengeParticipant.challenge_id == ch.id))).scalars().all())


async def tick(s: AsyncSession, ch: Challenge, quotes: dict[str, Quote], now: datetime | None = None) -> list[Event]:
    """Execute stops, re-value everyone, enforce the rules, record equity points and rank.
    The first tick after the end time finalises the challenge; later ticks change nothing."""
    now = now or _now()
    if ch.finalized:
        return []
    for q in quotes.values():
        _check_quote(q)
    ended = now >= _aware(ch.end_time)
    events: list[Event] = []
    rows = await participants(s, ch)
    for p in rows:
        if p.status != ACTIVE:
            continue
        for sym, h in list((p.holdings or {}).items()):
            if sym not in quotes:
                raise ChallengeError(f"no quote for {sym}")
            bid = quotes[sym].bid
            if bid <= h["stop"]:
                order, evs = _sell(s, ch, p, sym, h["qty"], bid, "stop", now)
                events.append(Event("stop_hit", p.id, p.user_id, ch.id, sym, bid, order.pnl,
                                    detail=f"stop {h['stop']}"))
                events += evs
            elif not h.get("warned") and bid <= h["stop"] * (1 + NEAR_STOP_PCT / 100):
                holdings = copy.deepcopy(p.holdings)
                holdings[sym]["warned"] = True
                p.holdings = holdings
                events.append(Event("near_stop", p.id, p.user_id, ch.id, sym, bid, detail=f"stop {h['stop']}"))
        mark_to_market(p, ch, quotes)
        _rollover_day(p, now)
        day_loss = (1 - p.equity / p.day_start_equity) * 100 if p.day_start_equity else 0.0
        if ch.daily_loss_limit_pct and day_loss > ch.daily_loss_limit_pct:
            events.append(_disqualify(s, ch, p, quotes, f"daily loss {day_loss:.2f}% > {ch.daily_loss_limit_pct}%",
                                      now))
        elif ch.max_drawdown_pct and p.max_drawdown_pct > ch.max_drawdown_pct:
            events.append(_disqualify(s, ch, p, quotes,
                                      f"drawdown {p.max_drawdown_pct:.2f}% > {ch.max_drawdown_pct}%", now))
        if p.last_point_at is None or now - _aware(p.last_point_at) >= POINT_EVERY:
            s.add(ChallengeEquityPoint(participant_id=p.id, ts=now, equity=p.equity))
            p.last_point_at = now
        if ended and p.status == ACTIVE:
            p.status = COMPLETED
            events.append(Event("completed", p.id, p.user_id, ch.id, detail=f"ROI {p.roi_pct:+.2f}%"))
    rank(rows)
    if ended:
        ch.finalized = True
    return events


def public_name(user: User) -> str:
    """Leaderboard name. Never shows e-mail addresses or Telegram ids."""
    if user.display_name:
        return user.display_name[:16]
    if user.email and not user.email.endswith("@telegram.invalid"):
        return user.email.split("@")[0][:3] + "***"
    return f"trader#{user.id}"
