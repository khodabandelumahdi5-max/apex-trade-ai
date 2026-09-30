"""Shared Pydantic v2 state schemas exchanged between the agents."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _State(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    timestamp: datetime = Field(default_factory=_now)


class AgentHealth(str, Enum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class OrderBookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)
    price: float = Field(gt=0)
    size: float = Field(ge=0)


class MarketDataState(_State):
    symbol: str = Field(min_length=1)
    mint: str | None = None
    price: float = Field(gt=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    depth_usd: float = Field(default=0.0, ge=0)
    order_book_imbalance: float | None = Field(default=None, ge=-1, le=1)
    liquidity_usd: float | None = Field(default=None, ge=0)
    price_change_24h_pct: float | None = None
    source: str = "jupiter"

    @model_validator(mode="after")
    def _check_book(self) -> "MarketDataState":
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(f"crossed book for {self.symbol}: bid {self.bid} > ask {self.ask}")
        if any(nxt.price > cur.price for cur, nxt in zip(self.bids, self.bids[1:])):
            raise ValueError("bids must be sorted by descending price")
        if any(nxt.price < cur.price for cur, nxt in zip(self.asks, self.asks[1:])):
            raise ValueError("asks must be sorted by ascending price")
        return self


class OnChainState(_State):
    token: str
    net_flow: float = Field(description="Net whale flow in USD over the window (+ = accumulation)")
    net_flow_pct: float = Field(default=0.0, description="Net flow as % of tracked whale holdings")
    whale_sentiment: Literal["ACCUMULATION", "DISTRIBUTION", "NEUTRAL", "UNKNOWN"]
    confidence: float = Field(ge=0, le=1)
    tracked_wallets: int = Field(default=0, ge=0)
    observations: int = Field(default=0, ge=0)
    method: str = "holders_delta"


class TimeframeAnalysis(BaseModel):
    timeframe: str
    close: float = Field(gt=0)
    ema_50: float = Field(gt=0)
    ema_200: float = Field(gt=0)
    rsi_14: float = Field(ge=0, le=100)
    atr_14: float = Field(ge=0)
    trend_score: float = Field(ge=-1, le=1)
    bars: int = Field(ge=0)


class TechnicalState(_State):
    symbol: str
    ema_50: float = Field(gt=0)
    ema_200: float = Field(gt=0)
    rsi_14: float = Field(ge=0, le=100)
    atr_14: float = Field(ge=0)
    order_book_imbalance: float = Field(default=0.0, ge=-1, le=1)
    confidence: float = Field(ge=0, le=1, description="Probability-like buy confidence score")
    signal: Literal["BUY", "SELL", "HOLD"]
    suggested_stop: float | None = Field(default=None, gt=0)
    tp1: float | None = Field(default=None, gt=0)
    tp2: float | None = Field(default=None, gt=0)
    timeframes: dict[str, TimeframeAnalysis] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class RiskState(_State):
    symbol: str
    approved: bool
    entry_price: float = Field(gt=0)
    position_size: float = Field(ge=0)
    notional_usd: float = Field(ge=0)
    max_risk_usd: float = Field(ge=0)
    stop_loss: float = Field(gt=0)
    is_zero_loss_armed: bool = False
    effective_risk_pct: float = Field(ge=0, le=2.0)
    kelly_fraction_used: float = Field(ge=0, le=1)
    kelly_raw: float
    portfolio_var_usd: float = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _stop_below_entry(self) -> "RiskState":
        if self.approved and self.stop_loss >= self.entry_price:
            raise ValueError("approved long position must have stop_loss < entry_price")
        return self


class ExecutionState(_State):
    symbol: str
    side: Literal["BUY", "SELL"]
    venue: Literal["paper", "jupiter", "cex", "mexc"]
    status: Literal["FILLED", "REJECTED", "FAILED", "SIMULATED"]
    order_id: str | None = None
    executed_price: float | None = Field(default=None, gt=0)
    size: float = Field(default=0.0, ge=0)
    fees_usd: float = Field(default=0.0, ge=0)
    pnl: float | None = None
    price_impact_pct: float | None = None
    error: str | None = None

    @field_validator("error")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        return v[:2000] if v else v


class PositionState(BaseModel):
    trade_id: int
    symbol: str
    mint: str | None
    venue: str
    entry_price: float = Field(gt=0)
    size: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    highest_price: float = Field(gt=0)
    last_price: float = Field(gt=0)
    protection_status: str
    initial_stop: float | None = Field(default=None, gt=0)
    tp1_hit: bool = False

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.entry_price) * self.size

    @property
    def unrealized_pct(self) -> float:
        return (self.last_price / self.entry_price - 1) * 100


class GlobalPortfolioState(_State):
    total_capital: float = Field(description="Equity: cash + marked open positions (USD)")
    cash: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    daily_pnl: float = 0.0
    active_trades_count: int = Field(ge=0)
    protected_trades_count: int = Field(default=0, ge=0)
    portfolio_var: float = Field(ge=0, description="1-day VaR in USD")
    portfolio_var_pct: float = Field(default=0.0, ge=0)
    peak_equity: float = Field(gt=0)
    current_drawdown: float = Field(ge=0, le=100, description="% below peak equity")
    halted: bool = False
    positions: list[PositionState] = Field(default_factory=list)
