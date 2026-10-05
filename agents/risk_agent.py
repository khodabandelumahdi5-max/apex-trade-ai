"""Institutional risk guardian: fractional-Kelly sizing capped at 1–2 % risk per trade,
portfolio VaR limits, circuit breakers and the Zero-Loss Breakeven Protocol."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd
from scipy.stats import norm

from agents.base_agent import BaseAgent
from config import Settings
from core.state import GlobalPortfolioState, PositionState, RiskState, TechnicalState
from database.models import ProtectionStatus, Trade

HARD_MAX_RISK_PCT = 2.0          # absolute ceiling, independent of any UI slider
PRIOR_WIN_RATE, PRIOR_PAYOFF, PRIOR_WEIGHT = 0.5, 1.5, 10
HOURS_PER_DAY = 24


@dataclass(frozen=True)
class EdgeEstimate:
    win_rate: float
    payoff: float
    samples: int

    @property
    def kelly(self) -> float:
        """Full Kelly fraction f* = p − (1 − p) / b, floored at 0."""
        if self.payoff <= 0:
            return 0.0
        return max(0.0, self.win_rate - (1 - self.win_rate) / self.payoff)


@dataclass(frozen=True)
class PositionUpdate:
    stop_loss: float
    protection_status: ProtectionStatus
    highest_price: float
    exit: bool
    reason: str | None = None
    partial_fraction: float = 0.0     # > 0: sell this share of the position now (TP1)

    def changed(self, pos: PositionState) -> bool:
        return (self.stop_loss != pos.stop_loss or self.protection_status.value != pos.protection_status
                or self.highest_price != pos.highest_price)


def estimate_edge(closed: Sequence[Trade]) -> EdgeEstimate:
    """Win rate and payoff ratio from closed trades, shrunk toward a conservative prior."""
    pnls = [t.pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    n = len(pnls)
    p = (len(wins) + PRIOR_WEIGHT * PRIOR_WIN_RATE) / (n + PRIOR_WEIGHT)
    if wins and losses:
        observed_b = float(np.mean(wins) / np.mean(losses))
        w = n / (n + PRIOR_WEIGHT)
        b = w * observed_b + (1 - w) * PRIOR_PAYOFF
    else:
        b = PRIOR_PAYOFF
    return EdgeEstimate(win_rate=p, payoff=b, samples=n)


def portfolio_var(exposures_usd: dict[str, float], hourly_returns: dict[str, pd.Series],
                  confidence: float = 0.95) -> float:
    """1-day VaR (USD), max of parametric (variance–covariance) and historical-simulation VaR."""
    symbols = [s for s, e in exposures_usd.items() if e != 0 and s in hourly_returns]
    if not symbols:
        return 0.0
    rets = pd.concat({s: hourly_returns[s] for s in symbols}, axis=1).dropna()
    if len(rets) < 48:
        # Too little overlap: conservative fallback of 10 % daily move per position, no diversification
        return float(sum(abs(exposures_usd[s]) for s in symbols) * 0.10)
    w = np.array([exposures_usd[s] for s in symbols])
    cov_daily = rets.cov().to_numpy() * HOURS_PER_DAY
    sigma = math.sqrt(max(float(w @ cov_daily @ w), 0.0))
    parametric = float(norm.ppf(confidence) * sigma)
    daily_pnl = rets.rolling(HOURS_PER_DAY).sum().dropna().to_numpy() @ w
    historical = float(-np.quantile(daily_pnl, 1 - confidence)) if len(daily_pnl) >= 30 else 0.0
    return max(parametric, historical, 0.0)


def take_profit_price(entry: float, stop: float, r_multiple: float) -> float | None:
    """Target at `r_multiple` × the initial risk above entry; None when disabled."""
    if r_multiple <= 0 or stop >= entry:
        return None
    return entry + r_multiple * (entry - stop)


def _reached(price: float, level: float) -> bool:
    # relative tolerance: (101.5/100 - 1)*100 == 1.4999999 in floats
    return price >= level or math.isclose(price, level, rel_tol=1e-9)


def manage_position(pos: PositionState, price: float, settings: Settings) -> PositionUpdate:
    """Exit management for a long position, evaluated on every price tick.

    * price ≥ TP2 (tp2_r × R above entry) → exit everything
    * price ≥ TP1 (first time) → sell `tp1_fraction`, stop moves to breakeven
    * unrealized ≥ +breakeven_trigger_pct → stop moves to entry (+ optional fee buffer)
    * after breakeven the stop trails `trailing_stop_pct` below the highest price, never moving down
    * price ≤ stop → exit
    """
    highest = max(pos.highest_price, price)
    stop = pos.stop_loss
    status = ProtectionStatus(pos.protection_status)
    initial = pos.initial_stop or pos.stop_loss
    tp1 = take_profit_price(pos.entry_price, initial, settings.tp1_r)
    tp2 = take_profit_price(pos.entry_price, initial, settings.tp2_r)
    tp1_now = tp1 is not None and not pos.tp1_hit and _reached(price, tp1)
    reached = _reached(price, pos.entry_price * (1 + settings.breakeven_trigger_pct / 100)) or tp1_now

    if reached and stop < pos.entry_price:
        stop = pos.entry_price * (1 + settings.breakeven_buffer_pct / 100)
        status = ProtectionStatus.BREAKEVEN_LOCKED
    if status != ProtectionStatus.INITIAL_STOP:
        trail = highest * (1 - settings.trailing_stop_pct / 100)
        if trail > stop:
            stop, status = trail, ProtectionStatus.TRAILING

    if tp2 is not None and _reached(price, tp2):
        return PositionUpdate(stop, status, highest, exit=True, reason="take_profit_2")
    if tp1_now:
        return PositionUpdate(stop, status, highest, exit=False, reason="take_profit_1",
                              partial_fraction=settings.tp1_fraction)
    if price <= stop:
        reason = {ProtectionStatus.INITIAL_STOP: "stop_loss", ProtectionStatus.BREAKEVEN_LOCKED: "breakeven_stop",
                  ProtectionStatus.TRAILING: "trailing_stop"}[status]
        return PositionUpdate(stop, status, highest, exit=True, reason=reason)
    return PositionUpdate(stop, status, highest, exit=False)


class RiskAgent(BaseAgent[RiskState]):
    name = "risk"

    def __init__(self, settings: Settings) -> None:
        super().__init__(timeout=30, retries=0)
        self.settings = settings

    def effective_risk_pct(self, slider_risk_pct: float, kelly_fraction: float, edge: EdgeEstimate) -> float:
        cap = min(HARD_MAX_RISK_PCT, max(0.0, slider_risk_pct))
        return min(cap, kelly_fraction * edge.kelly * 100)

    async def process(self, *, symbol: str, entry_price: float, technical: TechnicalState,  # type: ignore[override]
                      portfolio: GlobalPortfolioState, closed: Sequence[Trade],
                      hourly_returns: dict[str, pd.Series], risk_pct: float, kelly_fraction: float,
                      daily_start_equity: float) -> RiskState:
        s = self.settings
        reasons: list[str] = []
        edge = estimate_edge(closed)
        eff_risk = self.effective_risk_pct(risk_pct, kelly_fraction, edge)
        stop = technical.suggested_stop or entry_price * 0.97
        equity = portfolio.total_capital

        def reject(why: str) -> RiskState:
            reasons.append(why)
            return RiskState(symbol=symbol, approved=False, entry_price=entry_price, position_size=0,
                             notional_usd=0, max_risk_usd=0, stop_loss=max(stop, 1e-12),
                             effective_risk_pct=eff_risk, kelly_fraction_used=kelly_fraction,
                             kelly_raw=edge.kelly, portfolio_var_usd=portfolio.portfolio_var, reasons=reasons)

        reasons.append(f"edge p={edge.win_rate:.2f} b={edge.payoff:.2f} n={edge.samples} f*={edge.kelly:.3f}")
        if portfolio.halted:
            return reject("trading halted")
        if portfolio.active_trades_count >= s.max_open_positions:
            return reject(f"max open positions ({s.max_open_positions}) reached")
        if any(p.symbol == symbol for p in portfolio.positions):
            return reject("position already open for symbol")
        if daily_start_equity > 0 and (equity / daily_start_equity - 1) * 100 <= -s.daily_loss_limit_pct:
            return reject(f"daily loss limit {s.daily_loss_limit_pct}% hit")
        if portfolio.current_drawdown >= s.max_drawdown_pct:
            return reject(f"max drawdown {s.max_drawdown_pct}% hit")
        if eff_risk <= 0:
            return reject("no statistical edge (Kelly ≤ 0)")
        if stop >= entry_price:
            return reject("stop must be below entry")

        risk_per_unit = entry_price - stop
        size = equity * eff_risk / 100 / risk_per_unit
        caps = {"max_position_pct": equity * s.max_position_pct / 100 / entry_price,
                "available_cash": max(0.0, portfolio.cash * 0.98) / entry_price}
        for name, limit in caps.items():
            if size > limit:
                size = limit
                reasons.append(f"size capped by {name}")

        exposures = {p.symbol: p.size * p.last_price for p in portfolio.positions}
        exposures[symbol] = exposures.get(symbol, 0.0) + size * entry_price
        var_limit = equity * s.max_portfolio_var_pct / 100
        var_usd = portfolio_var(exposures, hourly_returns, s.var_confidence)
        if var_usd > var_limit and size > 0:
            # shrink the new position until the portfolio VaR fits (VaR is ~linear in size)
            base_exp = {k: v for k, v in exposures.items() if k != symbol}
            base_var = portfolio_var(base_exp, hourly_returns, s.var_confidence) if base_exp else 0.0
            if base_var >= var_limit:
                return reject(f"portfolio VaR ${base_var:,.0f} already ≥ limit ${var_limit:,.0f}")
            scale = (var_limit - base_var) / max(var_usd - base_var, 1e-9)
            size *= max(0.0, min(1.0, scale * 0.95))
            exposures[symbol] = size * entry_price
            var_usd = portfolio_var(exposures, hourly_returns, s.var_confidence)
            reasons.append(f"size scaled ×{scale:.2f} to respect VaR limit")

        notional = size * entry_price
        if notional < 5:
            return reject(f"position too small (${notional:.2f})")
        return RiskState(symbol=symbol, approved=True, entry_price=entry_price, position_size=size,
                         notional_usd=notional, max_risk_usd=size * risk_per_unit, stop_loss=stop,
                         is_zero_loss_armed=True, effective_risk_pct=eff_risk,
                         kelly_fraction_used=kelly_fraction, kelly_raw=edge.kelly,
                         portfolio_var_usd=var_usd, reasons=reasons)
