"""Competition rules. Prices come in as arguments so the rules are deterministic and testable.

* BUY fills at the ask, SELL at the bid, plus the challenge fee: the spread and fees are part of the game.
* Orders only between start_time and end_time, only for the challenge's symbols, never more than cash/holdings.
* Ranking: ROI on the identical starting balance; ties go to the lower max drawdown, then to who joined first.
  Participants without a single trade are listed but not ranked (holding cash is not a strategy).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Challenge, ChallengeOrder, ChallengeParticipant, User

MIN_ORDER_USD = 1.0


class ChallengeError(ValueError):
    pass


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def join(s: AsyncSession, challenge: Challenge, user: User, now: datetime | None = None) -> ChallengeParticipant:
    now = now or _now()
    if now >= _aware(challenge.end_time):
        raise ChallengeError("challenge has ended")
    if not user.subscription_active:
        raise ChallengeError("an active subscription is required to join")
    exists = (await s.execute(select(ChallengeParticipant).where(
        ChallengeParticipant.challenge_id == challenge.id, ChallengeParticipant.user_id == user.id))).scalar()
    if exists:
        return exists
    p = ChallengeParticipant(challenge_id=challenge.id, user_id=user.id, cash=challenge.starting_balance,
                             holdings={}, equity=challenge.starting_balance,
                             peak_equity=challenge.starting_balance, joined_at=now)
    s.add(p)
    await s.flush()
    return p


async def place_order(s: AsyncSession, challenge: Challenge, p: ChallengeParticipant, symbol: str, side: str,
                      quote: Quote, *, usd: float | None = None, qty: float | None = None,
                      now: datetime | None = None) -> ChallengeOrder:
    """Market order. BUY with `usd` to spend (fee included); SELL with `qty` (None = everything)."""
    now = now or _now()
    symbol, side = symbol.upper(), side.upper()
    if not _aware(challenge.start_time) <= now < _aware(challenge.end_time):
        raise ChallengeError("challenge is not running")
    if symbol not in challenge.symbol_list:
        raise ChallengeError(f"{symbol} is not traded in this challenge")
    if not 0 < quote.bid <= quote.ask:
        raise ChallengeError("invalid quote")
    fee_rate = challenge.fee_bps / 10_000
    holdings = dict(p.holdings or {})
    if side == "BUY":
        if usd is None or usd < MIN_ORDER_USD:
            raise ChallengeError(f"BUY needs usd ≥ {MIN_ORDER_USD}")
        if usd > p.cash + 1e-9:
            raise ChallengeError(f"not enough cash (${p.cash:,.2f})")
        fee = usd * fee_rate
        price = quote.ask
        filled = (usd - fee) / price
        p.cash -= usd
        holdings[symbol] = holdings.get(symbol, 0.0) + filled
    elif side == "SELL":
        held = holdings.get(symbol, 0.0)
        filled = held if qty is None else qty
        if filled <= 0 or filled > held * (1 + 1e-12):
            raise ChallengeError(f"cannot sell {filled} {symbol}, holding {held}")
        price = quote.bid
        gross = filled * price
        if gross < MIN_ORDER_USD and filled < held:
            raise ChallengeError(f"SELL below ${MIN_ORDER_USD}")
        fee = gross * fee_rate
        p.cash += gross - fee
        rest = held - filled
        if rest * price < 1e-6:
            holdings.pop(symbol, None)
        else:
            holdings[symbol] = rest
    else:
        raise ChallengeError("side must be BUY or SELL")
    p.holdings = holdings                       # new dict → SQLAlchemy sees the JSON change
    p.trades_count += 1
    order = ChallengeOrder(participant_id=p.id, symbol=symbol, side=side, qty=filled, price=price, fee_usd=fee,
                           created_at=now)
    s.add(order)
    return order


def mark_to_market(p: ChallengeParticipant, challenge: Challenge, quotes: dict[str, Quote]) -> None:
    """Value holdings at the bid (what they could be sold for) and update ROI / drawdown."""
    value = p.cash
    for sym, q in (p.holdings or {}).items():
        if sym not in quotes:
            raise ChallengeError(f"no quote for {sym}")
        value += q * quotes[sym].bid
    p.equity = value
    p.peak_equity = max(p.peak_equity, value)
    if p.peak_equity > 0:
        p.max_drawdown_pct = max(p.max_drawdown_pct, (1 - value / p.peak_equity) * 100)
    p.roi_pct = (value / challenge.starting_balance - 1) * 100


async def update_leaderboard(s: AsyncSession, challenge: Challenge, quotes: dict[str, Quote],
                             now: datetime | None = None) -> list[ChallengeParticipant]:
    """Re-value everyone and assign ranks. The first update after the end time is final."""
    now = now or _now()
    rows = (await s.execute(select(ChallengeParticipant)
                            .where(ChallengeParticipant.challenge_id == challenge.id))).scalars().all()
    if not challenge.finalized:
        for p in rows:
            mark_to_market(p, challenge, quotes)
        if now >= _aware(challenge.end_time):
            challenge.finalized = True          # one valuation after the end, then the result never changes
    ranked = sorted((p for p in rows if p.trades_count > 0),
                    key=lambda p: (-round(p.roi_pct, 6), round(p.max_drawdown_pct, 6), _aware(p.joined_at), p.id))
    for i, p in enumerate(ranked, start=1):
        p.rank = i
    for p in rows:
        if p.trades_count == 0:
            p.rank = None
    return ranked + [p for p in rows if p.trades_count == 0]

