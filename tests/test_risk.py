import numpy as np
import pandas as pd
import pytest

from agents.risk_agent import (HARD_MAX_RISK_PCT, EdgeEstimate, RiskAgent, estimate_edge, manage_position,
                               portfolio_var)
from config import Settings, WatchToken
from core.state import GlobalPortfolioState, PositionState, TechnicalState
from database.models import ProtectionStatus, Trade

SOL = WatchToken(symbol="SOL", mint="So11111111111111111111111111111111111111112")


def settings(**kw):
    return Settings(watchlist=[SOL], **kw)


def pos(entry=100.0, stop=97.0, highest=100.0, status="INITIAL_STOP"):
    return PositionState(trade_id=1, symbol="SOL", mint=SOL.mint, venue="paper", entry_price=entry, size=10,
                         stop_loss=stop, highest_price=highest, last_price=entry, protection_status=status)


def test_kelly_formula():
    assert EdgeEstimate(0.5, 1.5, 0).kelly == pytest.approx(0.5 - 0.5 / 1.5)
    assert EdgeEstimate(0.3, 1.0, 0).kelly == 0.0


def test_edge_prior_and_shrinkage():
    assert estimate_edge([]).win_rate == 0.5
    trades = [Trade(pnl=10.0) for _ in range(30)] + [Trade(pnl=-5.0) for _ in range(10)]
    e = estimate_edge(trades)
    assert 0.5 < e.win_rate < 0.75 and e.payoff > 1.5 and e.samples == 40


def test_breakeven_buffer_covers_fees():
    upd = manage_position(pos(), 101.5, settings())               # default buffer 0.25 %
    assert upd.stop_loss == pytest.approx(100.25)


def test_breakeven_triggers_exactly_at_threshold():
    s = settings(breakeven_buffer_pct=0.0)
    below = manage_position(pos(), 101.49, s)
    assert below.protection_status == ProtectionStatus.INITIAL_STOP and below.stop_loss == 97.0
    at = manage_position(pos(), 101.5, s)
    assert at.protection_status == ProtectionStatus.BREAKEVEN_LOCKED
    assert at.stop_loss == pytest.approx(100.0) and not at.exit


def test_trailing_never_moves_down_and_exits():
    s = settings(trailing_stop_pct=2.0)
    up = manage_position(pos(stop=100.0, highest=101.5, status="BREAKEVEN_LOCKED"), 110.0, s)
    assert up.protection_status == ProtectionStatus.TRAILING and up.stop_loss == pytest.approx(107.8)
    p2 = pos(stop=up.stop_loss, highest=110.0, status="TRAILING")
    down = manage_position(p2, 108.0, s)
    assert down.stop_loss == pytest.approx(107.8) and not down.exit
    hit = manage_position(p2, 107.5, s)
    assert hit.exit and hit.reason == "trailing_stop"


def test_initial_stop_exit():
    out = manage_position(pos(), 96.9, settings())
    assert out.exit and out.reason == "stop_loss"


def _portfolio(equity=10_000.0, cash=10_000.0, **kw):
    return GlobalPortfolioState(total_capital=equity, cash=cash, active_trades_count=0, portfolio_var=0,
                                peak_equity=equity, current_drawdown=0, **kw)


def _tech(stop):
    return TechnicalState(symbol="SOL", ema_50=100, ema_200=95, rsi_14=50, atr_14=1.5, confidence=0.8,
                          signal="BUY", suggested_stop=stop)


def _returns(vol=0.01, n=500, seed=1):
    rng = np.random.default_rng(seed)
    return {"SOL": pd.Series(rng.normal(0, vol, n))}


@pytest.mark.asyncio
async def test_risk_caps_and_hard_ceiling(monkeypatch):
    agent = RiskAgent(settings(max_portfolio_var_pct=50))
    monkeypatch.setattr(agent, "_beat", _noop)
    r = await agent.run(symbol="SOL", entry_price=100.0, technical=_tech(99.9), portfolio=_portfolio(), closed=[],
                        hourly_returns=_returns(), risk_pct=5.0, kelly_fraction=1.0, daily_start_equity=10_000)
    assert r.approved
    assert r.effective_risk_pct <= HARD_MAX_RISK_PCT
    assert r.notional_usd <= 10_000 * 0.25 + 1e-6          # max_position_pct cap beats tight stop
    assert r.max_risk_usd <= 10_000 * HARD_MAX_RISK_PCT / 100


@pytest.mark.asyncio
async def test_risk_one_percent_and_var_scaling(monkeypatch):
    agent = RiskAgent(settings(max_portfolio_var_pct=1.0))
    monkeypatch.setattr(agent, "_beat", _noop)
    r = await agent.run(symbol="SOL", entry_price=100.0, technical=_tech(95.0), portfolio=_portfolio(), closed=[],
                        hourly_returns=_returns(vol=0.02), risk_pct=1.0, kelly_fraction=0.25,
                        daily_start_equity=10_000)
    assert r.approved and r.max_risk_usd <= 100.0 + 1e-6
    assert r.portfolio_var_usd <= 100.0 * 1.01               # 1 % of equity VaR limit


@pytest.mark.asyncio
async def test_risk_rejects_on_drawdown_and_halt(monkeypatch):
    agent = RiskAgent(settings())
    monkeypatch.setattr(agent, "_beat", _noop)
    common = dict(symbol="SOL", entry_price=100.0, technical=_tech(97), closed=[], hourly_returns=_returns(),
                  risk_pct=1.0, kelly_fraction=0.25, daily_start_equity=10_000)
    r = await agent.run(portfolio=_portfolio(halted=True), **common)
    assert not r.approved and "halted" in r.reasons[-1]
    dd = GlobalPortfolioState(total_capital=8_900, cash=8_900, active_trades_count=0, portfolio_var=0,
                              peak_equity=10_000, current_drawdown=11)
    r = await agent.run(portfolio=dd, **common)
    assert not r.approved


def test_var_monotonic_in_exposure():
    rets = _returns()
    assert portfolio_var({"SOL": 2000}, rets) > portfolio_var({"SOL": 1000}, rets) > 0


async def _noop(*a, **k):
    return None


def test_tp1_partial_then_tp2_full_exit():
    s = settings(tp1_r=1.5, tp1_fraction=0.5, tp2_r=3.0)
    p = pos(entry=100, stop=98, highest=100)                    # R = 2 → TP1 103, TP2 106
    p = p.model_copy(update={"initial_stop": 98.0})
    upd = manage_position(p, 102.9, s)
    assert upd.partial_fraction == 0 and not upd.exit
    upd = manage_position(p, 103.0, s)
    assert upd.partial_fraction == 0.5 and upd.reason == "take_profit_1"
    assert upd.stop_loss >= 100.0                                # risk-free after TP1
    after = p.model_copy(update={"tp1_hit": True, "stop_loss": upd.stop_loss,
                                 "protection_status": upd.protection_status.value, "highest_price": 103.0})
    assert manage_position(after, 104.0, s).partial_fraction == 0    # TP1 fires only once
    final = manage_position(after, 106.0, s)
    assert final.exit and final.reason == "take_profit_2"


def test_targets_can_be_disabled():
    s = settings(tp1_r=0, tp2_r=0)
    p = pos(entry=100, stop=98).model_copy(update={"initial_stop": 98.0})
    upd = manage_position(p, 150.0, s)
    assert not upd.exit and upd.partial_fraction == 0
