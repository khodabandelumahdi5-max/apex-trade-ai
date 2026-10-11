from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from competition import engine
from competition.engine import ChallengeError, Quote, RuleViolation
from database.connection import dispose_engine, get_session, init_db
from database.models import Challenge, ChallengeEquityPoint, User

T0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
H1 = T0 + timedelta(hours=1)
SOL = Quote(99.9, 100.0)


@pytest_asyncio.fixture
async def db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/comp.db")
    yield
    await dispose_engine()


async def _setup(s, n=1, **rules):
    ch = Challenge(title="t", symbols="SOL,JUP", starting_balance=10_000, fee_bps=10,
                   start_time=T0, end_time=T0 + timedelta(days=21), **rules)
    s.add(ch)
    users = [User(email=f"u{i}@x.co", hashed_password="!", subscription_expires_at=datetime.now(timezone.utc)
                  + timedelta(days=30)) for i in range(n)]
    s.add_all(users)
    await s.flush()
    ps = [await engine.join(s, ch, u, now=T0 + timedelta(minutes=i)) for i, u in enumerate(users)]
    return ch, users, ps


def test_stop_loss_risk_includes_both_fees():
    # 10 units bought for $1001 (incl. fee), stop 95 → proceeds 950 × 0.999
    assert engine.stop_loss_risk(10, 1001, 95, 0.001) == pytest.approx(1001 - 949.05)


@pytest.mark.asyncio
async def test_buy_requires_stop_and_risk_budget(db):
    async with get_session() as s:
        ch, _, (p,) = await _setup(s)
        for kw in ({"stop": None}, {"stop": 99.99}):                        # missing / too close
            with pytest.raises(RuleViolation):
                await engine.buy(s, ch, p, "SOL", SOL, usd=1000, now=H1, **kw)
        # 1 % of 10 000 = $100 budget; $5000 with a 5 % stop risks ≈ $260
        with pytest.raises(RuleViolation, match="at most"):
            await engine.buy(s, ch, p, "SOL", SOL, usd=5000, stop=95, now=H1)
        assert p.rule_breaks == 3 and engine.discipline_score(p) == 70
        o = await engine.buy(s, ch, p, "SOL", SOL, usd=1800, stop=95, now=H1)
        h = p.holdings["SOL"]
        assert o.price == 100.0 and h["qty"] == pytest.approx(1798.2 / 100)
        assert engine.stop_loss_risk(h["qty"], h["cost"], 95, 0.001) <= 100
        assert p.cash == pytest.approx(8200)
        # plain refusals are not rule breaks
        with pytest.raises(ChallengeError) as exc:
            await engine.buy(s, ch, p, "BTC", SOL, usd=10, stop=1, now=H1)
        assert not isinstance(exc.value, RuleViolation) and p.rule_breaks == 3


@pytest.mark.asyncio
async def test_rule_break_is_saved_when_caught_inside_session(db):
    async with get_session() as s:
        ch, _, (p,) = await _setup(s)
        pid = p.id
        try:
            await engine.buy(s, ch, p, "SOL", SOL, usd=100, stop=None, now=H1)
        except RuleViolation:
            pass
    async with get_session() as s:
        from database.models import ChallengeParticipant
        assert (await s.get(ChallengeParticipant, pid)).rule_breaks == 1


@pytest.mark.asyncio
async def test_stops_only_go_up_and_execute_at_bid(db):
    async with get_session() as s:
        ch, _, (p,) = await _setup(s)
        await engine.buy(s, ch, p, "SOL", SOL, usd=1000, stop=95, now=H1)
        with pytest.raises(RuleViolation):
            await engine.set_stop(ch, p, "SOL", 90, SOL, now=H1)
        await engine.set_stop(ch, p, "SOL", 96, SOL, now=H1)
        ev = await engine.tick(s, ch, {"SOL": Quote(96.8, 96.9), "JUP": Quote(1, 1)}, now=H1)
        assert [e.kind for e in ev] == ["near_stop"]
        assert await engine.tick(s, ch, {"SOL": Quote(96.8, 96.9), "JUP": Quote(1, 1)}, now=H1) == []   # once
        ev = await engine.tick(s, ch, {"SOL": Quote(95.0, 95.1), "JUP": Quote(1, 1)}, now=H1)  # gapped below 96
        assert ev[0].kind == "stop_hit" and ev[0].price == 95.0 and ev[0].pnl < 0
        assert "SOL" not in p.holdings and p.losses == 1 and p.consecutive_losses == 1


@pytest.mark.asyncio
async def test_cooldown_after_consecutive_losses_and_overtrading(db):
    async with get_session() as s:
        ch, _, (p,) = await _setup(s, max_consecutive_losses=2, cooldown_hours=24, max_entries_per_day=3)
        events = []
        for _ in range(2):
            await engine.buy(s, ch, p, "SOL", SOL, usd=500, stop=95, now=H1)
            _, ev = await engine.sell(s, ch, p, "SOL", Quote(99.0, 99.1), now=H1)
            events += ev
        assert [e.kind for e in events] == ["cooldown"]
        with pytest.raises(RuleViolation, match="cooldown"):
            await engine.buy(s, ch, p, "SOL", SOL, usd=500, stop=95, now=H1)
        later = H1 + timedelta(hours=25)
        await engine.buy(s, ch, p, "SOL", SOL, usd=100, stop=95, now=later)       # new day, cooldown over
        await engine.buy(s, ch, p, "JUP", Quote(0.99, 1.0), usd=100, stop=0.95, now=later)
        await engine.buy(s, ch, p, "SOL", SOL, usd=100, stop=95, now=later)
        with pytest.raises(RuleViolation, match="overtrading"):
            await engine.buy(s, ch, p, "JUP", Quote(0.99, 1.0), usd=100, stop=0.95, now=later)
        # selling is always allowed
        await engine.sell(s, ch, p, "JUP", Quote(0.99, 1.0), now=later)


@pytest.mark.asyncio
async def test_daily_loss_disqualifies_and_liquidates(db):
    async with get_session() as s:
        ch, _, (p,) = await _setup(s, max_risk_per_trade_pct=0)          # risk rule off to allow a big position
        await engine.buy(s, ch, p, "SOL", SOL, usd=9000, stop=50, now=H1)
        await engine.tick(s, ch, {"SOL": SOL, "JUP": Quote(1, 1)}, now=H1)      # day start ≈ 9 990
        ev = await engine.tick(s, ch, {"SOL": Quote(96.0, 96.1), "JUP": Quote(1, 1)}, now=H1 + timedelta(minutes=1))
        assert ev[-1].kind == "disqualified" and "daily loss" in ev[-1].detail
        assert p.status == "DISQUALIFIED" and p.holdings == {} and p.rank is None
        with pytest.raises(ChallengeError):
            await engine.buy(s, ch, p, "SOL", SOL, usd=10, stop=95, now=H1)


@pytest.mark.asyncio
async def test_drawdown_disqualifies_across_days(db):
    async with get_session() as s:
        ch, _, (p,) = await _setup(s, max_risk_per_trade_pct=0, daily_loss_limit_pct=0, max_drawdown_pct=5)
        await engine.buy(s, ch, p, "SOL", SOL, usd=9000, stop=10, now=H1)
        q = {"JUP": Quote(1, 1)}
        await engine.tick(s, ch, {**q, "SOL": Quote(97.0, 97.1)}, now=H1)
        assert p.status == "ACTIVE"
        await engine.tick(s, ch, {**q, "SOL": Quote(93.0, 93.1)}, now=H1 + timedelta(days=1))
        assert p.status == "DISQUALIFIED" and "drawdown" in p.status_reason


@pytest.mark.asyncio
async def test_ranking_discipline_first_then_roi_and_final_freeze(db):
    async with get_session() as s:
        ch, _, (a, b, c, d) = await _setup(s, n=4)
        await engine.buy(s, ch, a, "SOL", SOL, usd=1500, stop=95, now=H1)
        await engine.buy(s, ch, b, "SOL", SOL, usd=1000, stop=95, now=H1)
        await engine.buy(s, ch, c, "SOL", SOL, usd=1900, stop=95, now=H1)
        with pytest.raises(RuleViolation):
            await engine.buy(s, ch, c, "SOL", SOL, usd=100, stop=None, now=H1)    # c breaks a rule once
        up = {"SOL": Quote(110.0, 110.1), "JUP": Quote(1, 1)}
        await engine.tick(s, ch, up, now=H1)
        assert (a.rank, b.rank, c.rank, d.rank) == (1, 2, 3, None)    # c has the best ROI but discipline 90
        end = T0 + timedelta(days=21, minutes=1)
        ev = await engine.tick(s, ch, up, now=end)
        assert ch.finalized and a.status == "COMPLETED" and {e.kind for e in ev} == {"completed"}
        roi = a.roi_pct
        assert await engine.tick(s, ch, {"SOL": Quote(1, 1.1), "JUP": Quote(1, 1)}, now=end + timedelta(days=1)) == []
        assert a.roi_pct == roi and a.rank == 1
        points = (await s.execute(select(func.count()).select_from(ChallengeEquityPoint))).scalar()
        assert points == 8                                             # 4 participants × 2 ticks ≥ 15 min apart


@pytest.mark.asyncio
async def test_join_requires_subscription_and_open_challenge(db):
    async with get_session() as s:
        ch, (u,), _ = await _setup(s)
        late = User(email="late@x.co", hashed_password="!", subscription_expires_at=datetime.now(timezone.utc)
                    - timedelta(days=1))
        s.add(late)
        await s.flush()
        with pytest.raises(ChallengeError, match="subscription"):
            await engine.join(s, ch, late, now=T0)
        late.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        with pytest.raises(ChallengeError, match="ended"):
            await engine.join(s, ch, late, now=T0 + timedelta(days=21))
