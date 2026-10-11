from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from competition.engine import ChallengeError, Quote, join, place_order, update_leaderboard
from database.connection import dispose_engine, get_session, init_db
from database.models import Challenge, User

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
Q = {"SOL": Quote(99.0, 100.0), "JUP": Quote(0.49, 0.50)}


@pytest_asyncio.fixture
async def db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/comp.db")
    yield
    await dispose_engine()


async def _setup(s, n=3):
    ch = Challenge(title="t", symbols="SOL,JUP", starting_balance=10_000, fee_bps=10,
                   start_time=T0, end_time=T0 + timedelta(days=7))
    s.add(ch)
    users = [User(email=f"u{i}@x.co", hashed_password="x", subscription_expires_at=datetime.now(timezone.utc)
                  + timedelta(days=30)) for i in range(n)]
    s.add_all(users)
    await s.flush()
    return ch, users


@pytest.mark.asyncio
async def test_fills_at_bid_ask_with_fees_and_limits(db):
    async with get_session() as s:
        ch, (u, *_) = await _setup(s)
        p = await join(s, ch, u, now=T0)
        assert await join(s, ch, u, now=T0) is p                       # joining twice is a no-op
        now = T0 + timedelta(hours=1)
        o = await place_order(s, ch, p, "sol", "buy", Q["SOL"], usd=1000, now=now)
        assert o.price == 100.0 and o.qty == pytest.approx(999 / 100) and p.cash == pytest.approx(9000)
        with pytest.raises(ChallengeError):
            await place_order(s, ch, p, "SOL", "BUY", Q["SOL"], usd=9001, now=now)       # more than cash
        with pytest.raises(ChallengeError):
            await place_order(s, ch, p, "BTC", "BUY", Q["SOL"], usd=10, now=now)         # not in challenge
        with pytest.raises(ChallengeError):
            await place_order(s, ch, p, "JUP", "SELL", Q["JUP"], qty=1, now=now)         # nothing to sell
        with pytest.raises(ChallengeError):
            await place_order(s, ch, p, "SOL", "BUY", Q["SOL"], usd=10, now=T0 + timedelta(days=8))  # ended
        o = await place_order(s, ch, p, "SOL", "SELL", Q["SOL"], now=now)                # sell all at bid
        assert o.price == 99.0 and "SOL" not in p.holdings
        # round trip at the spread + 2 fees loses money
        assert p.cash == pytest.approx(9000 + 9.99 * 99 * 0.999)
        assert p.cash < 10_000


@pytest.mark.asyncio
async def test_ranking_ties_unranked_and_freeze(db):
    async with get_session() as s:
        ch, (a, b, c) = await _setup(s)
        pa, pb, pc = [await join(s, ch, u, now=T0 + timedelta(minutes=i)) for i, u in enumerate((a, b, c))]
        now = T0 + timedelta(hours=1)
        await place_order(s, ch, pa, "SOL", "BUY", Q["SOL"], usd=5000, now=now)
        await place_order(s, ch, pb, "JUP", "BUY", Q["JUP"], usd=5000, now=now)
        up = {"SOL": Quote(120.0, 121.0), "JUP": Quote(0.40, 0.41)}
        rows = await update_leaderboard(s, ch, up, now=now)
        assert [p.user_id for p in rows] == [a.id, b.id, c.id]
        assert (pa.rank, pb.rank, pc.rank) == (1, 2, None)              # no trades → not ranked
        assert pa.roi_pct > 0 > pb.roi_pct
        # drawdown is tracked from the peak
        await update_leaderboard(s, ch, {"SOL": Quote(100.0, 101.0), "JUP": Q["JUP"]}, now=now)
        assert pa.max_drawdown_pct > 0
        # first update after the end is final; later prices don't change the result
        end = T0 + timedelta(days=7, minutes=1)
        await update_leaderboard(s, ch, up, now=end)
        final_roi = pa.roi_pct
        assert ch.finalized
        await update_leaderboard(s, ch, {"SOL": Quote(1.0, 1.1), "JUP": Q["JUP"]}, now=end + timedelta(days=1))
        assert pa.roi_pct == final_roi and pa.rank == 1


@pytest.mark.asyncio
async def test_join_requires_subscription_and_open_challenge(db):
    async with get_session() as s:
        ch, (u, *_) = await _setup(s)
        u.subscription_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        with pytest.raises(ChallengeError):
            await join(s, ch, u, now=T0)
        u.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        with pytest.raises(ChallengeError):
            await join(s, ch, u, now=T0 + timedelta(days=7))
