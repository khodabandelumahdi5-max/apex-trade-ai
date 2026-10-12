from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from config import Settings, WatchToken
from core.state import MarketDataState, TechnicalState
from database.connection import dispose_engine, get_session, init_db
from database.models import TradeSignal
from signalbot import service as sigsvc
from signalbot.core import (SignalConfig, evaluate_idea, evaluate_position, new_signal, position_size, signal_text,
                            trend_alert)

T0 = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
SET = Settings(watchlist=[WatchToken(symbol="SOL", mint="x")])          # TP1 1.5R, TP2 3R, BE +1.5 %, trail 2 %
CFG = SignalConfig(symbols=["SOL", "BTC"], budget_usd=1000, risk_pct=1.0, fee_pct=0.1)


def sig(entry=100.0, stop=96.0):
    s = new_signal("SOL", entry, stop, 0.8, ["conf=0.80"], SET, CFG, T0)
    s.id = 1
    return s


def entered(entry=100.0, stop=96.0):
    s = sig(entry, stop)
    sigsvc.enter(s, entry, SET, CFG, T0)
    return s


def test_size_respects_risk_and_budget():
    qty, usd, loss = position_size(CFG, 100.0, 96.0)
    assert loss == pytest.approx(10.0)                         # 1 % of $1000, fees included
    assert usd <= 1000
    qty, usd, loss = position_size(CFG, 100.0, 99.5)          # tight stop → capped by the budget, not leverage
    assert usd <= 1000 / 1.001 + 1e-6 and loss < 10
    s = sig()
    assert s.tp1 == pytest.approx(106.0) and s.tp2 == pytest.approx(112.0)
    assert new_signal("SOL", 100, 99.9, 0.8, [], SET, CFG, T0) is None     # stop < 0.5 % away: noise
    assert new_signal("SOL", 100, 80, 0.8, [], SET, CFG, T0) is None       # stop > 15 % away
    text = signal_text(s, CFG)
    assert "tradingview.com/chart/?symbol=MEXC:SOLUSDT" in text and "حد ضرر" in text


def test_idea_invalidation_expiry_and_chase():
    s = sig()
    assert evaluate_idea(s, 99.9, 100.0, CFG, T0) == []
    assert [a.kind for a in evaluate_idea(s, 101.0, 101.2, CFG, T0)] == ["chase"]
    assert evaluate_idea(s, 101.0, 101.2, CFG, T0) == []                  # once
    assert [a.kind for a in evaluate_idea(s, 95.9, 96.0, CFG, T0)] == ["invalidated"] and s.status == "EXPIRED"
    s2 = sig()
    assert [a.kind for a in evaluate_idea(s2, 99.9, 100, CFG, T0 + timedelta(hours=3, minutes=1))] == ["expired"]


def test_danger_warnings_fire_once_and_rearm():
    s = entered()
    assert evaluate_position(s, 99.0, SET, T0) == []                       # 25 % of the way
    assert [a.kind for a in evaluate_position(s, 98.0, SET, T0)] == ["danger"]           # 50 %
    assert evaluate_position(s, 97.9, SET, T0) == []
    a = evaluate_position(s, 96.7, SET, T0)                                # 82 %
    assert [x.kind for x in a] == ["danger"] and "80%" in a[0].text
    assert evaluate_position(s, 99.5, SET, T0) == [] and s.danger_level == 0             # recovered → re-armed
    assert [a.kind for a in evaluate_position(s, 97.9, SET, T0)] == ["danger"]
    out = evaluate_position(s, 95.9, SET, T0)
    assert out[0].kind == "exit" and "حد ضرر" in out[0].text and s.status == "CLOSED"
    assert s.close_reason == "stop_loss" and s.exit_price == 95.9


def test_breakeven_tp1_trailing_and_protected_exit():
    s = entered()
    a = evaluate_position(s, 101.6, SET, T0)                               # +1.6 % → breakeven
    assert [x.kind for x in a] == ["move_stop"] and s.stop == pytest.approx(100.25)
    a = evaluate_position(s, 106.0, SET, T0)                               # TP1 (1.5R)
    assert [x.kind for x in a] == ["tp1"] and s.tp1_hit and s.stop >= 100.25
    a = evaluate_position(s, 108.0, SET, T0)                               # trail 2 % below the high
    assert [x.kind for x in a] == ["move_stop"] and s.stop == pytest.approx(105.84)
    assert evaluate_position(s, 108.05, SET, T0) == []                     # tiny raise: no spam
    out = evaluate_position(s, 105.5, SET, T0)
    assert out[0].kind == "exit" and s.close_reason == "trailing_stop" and s.status == "CLOSED"


def test_tp2_and_trend_exit():
    s = entered()
    out = evaluate_position(s, 112.5, SET, T0)
    assert out[-1].kind == "exit" and s.close_reason == "take_profit_2"
    t = entered()
    assert [a.kind for a in trend_alert(t, "SELL", 99.0)] == ["trend"]
    assert trend_alert(t, "SELL", 99.0) == []                              # once
    assert trend_alert(sig(), "SELL", 99.0) == []                          # not entered → no exit alert


def test_enter_and_close_rules():
    s = sig()
    with pytest.raises(sigsvc.SignalError):
        sigsvc.enter(s, 95.0, SET, CFG, T0)                                # below the stop
    text = sigsvc.enter(s, 101.5, SET, CFG, T0)                            # bought higher than the signal
    assert "بالاتر" in text and s.tp1 == pytest.approx(101.5 + 1.5 * 5.5)
    with pytest.raises(sigsvc.SignalError):
        sigsvc.enter(s, 101.0, SET, CFG, T0)                               # already entered
    assert "+0.99%" in sigsvc.close(s, 102.5, "manual", T0)
    with pytest.raises(sigsvc.SignalError):
        sigsvc.close(s, 102.5, "manual", T0)


@pytest_asyncio.fixture
async def db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/sig.db")
    yield
    await dispose_engine()


def _tech(signal="BUY", stop=96.0, conf=0.8):
    return TechnicalState(symbol="SOL", ema_50=99, ema_200=95, rsi_14=50, atr_14=2, confidence=conf, signal=signal,
                          suggested_stop=stop, reasons=["conf"])


@pytest.mark.asyncio
async def test_scan_creates_one_signal_per_cooldown_and_survives_errors(db):
    state = {"signal": "BUY"}

    async def tech_fn(sym):
        if sym == "BTC":
            raise RuntimeError("timeout")
        return _tech(state["signal"]), MarketDataState(symbol=sym, price=100, bid=99.9, ask=100.0)

    async with get_session() as s:
        created, alerts, errors = await sigsvc.scan(s, tech_fn, SET, CFG, T0)
        assert [x.symbol for x in created] == ["SOL"] and errors == ["BTC: timeout"]
        created, _, _ = await sigsvc.scan(s, tech_fn, SET, CFG, T0 + timedelta(minutes=5))
        assert created == []                                               # already active
        sigsvc.close(created and created[0] or (await sigsvc.active_signals(s))[0], None, "skipped", T0)
        created, _, _ = await sigsvc.scan(s, tech_fn, SET, CFG, T0 + timedelta(hours=2))
        assert created == []                                               # 12 h cooldown
        created, _, _ = await sigsvc.scan(s, tech_fn, SET, CFG, T0 + timedelta(hours=13))
        assert len(created) == 1
        sigsvc.enter(created[0], 100.0, SET, CFG, T0)
        state["signal"] = "SELL"
        _, alerts, _ = await sigsvc.scan(s, tech_fn, SET, CFG, T0 + timedelta(hours=14))
        assert [a.kind for a in alerts] == ["trend"]


@pytest.mark.asyncio
async def test_watch_uses_one_quote_per_symbol(db):
    calls = []

    async def quote_fn(sym):
        calls.append(sym)
        return 97.9, 98.0

    async with get_session() as s:
        a = new_signal("SOL", 100, 96, 0.8, [], SET, CFG, T0)
        b = new_signal("SOL", 100, 96, 0.8, [], SET, CFG, T0)
        s.add_all([a, b])
        await s.flush()
        sigsvc.enter(a, 100.0, SET, CFG, T0)
        alerts = await sigsvc.watch(s, quote_fn, SET, CFG, T0)
        assert calls == ["SOL"] and [x.kind for x in alerts] == ["danger"]
        assert len((await s.execute(select(TradeSignal))).scalars().all()) == 2
