"""Event-driven backtest of the technical + risk logic, reusing the live agent functions.

Assumptions (see README "Backtest"):
* Signals are evaluated on each closed 1h bar; entries fill at the next bar's open plus slippage.
* 4h frames are resampled from 1h (completed bars only). The 15m trend input is replaced by the 1h
  trend because the public API only serves ~2 months of 15m history.
* On-chain whale flow and order-book imbalance have no history → treated as neutral (OBI = 0, on-chain
  gate passes). The backtest therefore measures the technical + risk layers only.
* Stops are checked against each bar's low before the breakeven/trailing update from its high
  (conservative ordering); a gap below the stop fills at the open.
"""
from __future__ import annotations

import argparse
import asyncio
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from agents.risk_agent import HARD_MAX_RISK_PCT, estimate_edge, manage_position
from agents.technical_agent import analyse_frame, confidence_score
from backtest.data import load, load_mexc
from config import Settings, WatchToken
from core.state import PositionState
from database.models import ProtectionStatus, Trade

LOOKBACK = 500


@dataclass
class BTConfig:
    buy_threshold: float = 0.65
    breakeven: bool = True
    technical_exit: bool = True
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    risk_pct: float = 1.0
    kelly_fraction: float = 0.25
    max_position_pct: float = 25.0
    atr_mult: float = 2.0
    trailing_pct: float = 2.0
    breakeven_trigger_pct: float = 1.5
    capital: float = 10_000.0
    tp1_r: float = 0.0        # 0 = off (the original strategy)
    tp1_fraction: float = 0.5
    tp2_r: float = 0.0


@dataclass
class BTTrade:
    entry_time: pd.Timestamp
    entry: float
    size: float
    stop: float
    initial_stop: float
    highest: float
    status: ProtectionStatus = ProtectionStatus.INITIAL_STOP
    tp1_hit: bool = False
    realized_partial: float = 0.0
    exit_time: pd.Timestamp | None = None
    exit: float | None = None
    reason: str | None = None
    pnl: float = 0.0
    fees: float = 0.0


@dataclass
class BTResult:
    symbol: str
    config: BTConfig
    trades: list[BTTrade]
    equity: pd.Series
    benchmark: pd.Series
    metrics: dict[str, float] = field(default_factory=dict)


def _signals(h1: pd.DataFrame, i: int, cfg: BTConfig) -> tuple[str, float, float]:
    """Signal on bar i (closed) using only data up to and including bar i."""
    win = h1.iloc[max(0, i - LOOKBACK * 4 + 1): i + 1]
    f1 = analyse_frame(win.iloc[-LOOKBACK:], "1h")
    h4 = (win.set_index("timestamp").resample("4h", label="left", closed="left")
          .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna())
    last_ts = win["timestamp"].iloc[-1]
    if h4.index[-1] + pd.Timedelta(hours=4) > last_ts + pd.Timedelta(hours=1):
        h4 = h4.iloc[:-1]  # drop the still-forming 4h bar
    f4 = analyse_frame(h4.reset_index().iloc[-LOOKBACK:], "4h")
    frames = {"15m": f1, "1h": f1, "4h": f4}
    conf = confidence_score(frames, 0.0)
    if conf >= cfg.buy_threshold and f4.trend_score > 0 and f1.ema_50 > f1.ema_200 and f1.rsi_14 < 70:
        sig = "BUY"
    elif f1.close < f1.ema_50 < f1.ema_200:
        sig = "SELL"
    else:
        sig = "HOLD"
    return sig, conf, f1.atr_14


def run_backtest(symbol: str, h1: pd.DataFrame, cfg: BTConfig) -> BTResult:
    h1 = h1.sort_values("timestamp").reset_index(drop=True)
    settings = Settings(watchlist=[WatchToken(symbol=symbol, mint="x")], trailing_stop_pct=cfg.trailing_pct,
                        breakeven_trigger_pct=cfg.breakeven_trigger_pct if cfg.breakeven else 1e6,
                        tp1_r=cfg.tp1_r, tp1_fraction=cfg.tp1_fraction, tp2_r=cfg.tp2_r)
    fee, slip = cfg.fee_bps / 1e4, cfg.slippage_bps / 1e4
    cash, pos = cfg.capital, None
    closed: list[BTTrade] = []
    equity: list[tuple[pd.Timestamp, float]] = []
    warmup = 4 * 210  # enough 1h bars for 200+ completed 4h bars
    pending_buy: tuple[float, float] | None = None  # (stop_distance, conf)
    pending_sell = False

    def close(t: BTTrade, ts: pd.Timestamp, price: float, reason: str) -> None:
        nonlocal cash
        fill = price * (1 - slip)
        proceeds = fill * t.size
        t.fees += proceeds * fee
        cash += proceeds - proceeds * fee
        t.exit_time, t.exit, t.reason = ts, fill, reason
        t.pnl = proceeds - t.entry * t.size - t.fees + t.realized_partial
        closed.append(t)

    for i in range(warmup, len(h1)):
        bar = h1.iloc[i]
        ts, o, hi, lo, c = bar.timestamp, bar.open, bar.high, bar.low, bar.close

        # 1) orders decided on the previous close fill at this bar's open
        if pos is not None and pending_sell:
            close(pos, ts, o, "technical_exit")
            pos = None
        if pos is None and pending_buy is not None:
            stop_dist, _ = pending_buy
            fill = o * (1 + slip)
            stop = fill - stop_dist
            equity_now = cash
            edge = estimate_edge([Trade(pnl=t.pnl) for t in closed])
            eff_risk = min(cfg.risk_pct, HARD_MAX_RISK_PCT, cfg.kelly_fraction * edge.kelly * 100)
            if eff_risk > 0 and stop > 0:
                size = equity_now * eff_risk / 100 / stop_dist
                size = min(size, equity_now * cfg.max_position_pct / 100 / fill, cash * 0.98 / fill)
                if size * fill >= 10:
                    cost = size * fill
                    fees = cost * fee
                    cash -= cost + fees
                    pos = BTTrade(ts, fill, size, stop, stop, fill, fees=fees)
        pending_buy, pending_sell = None, False

        # 2) intrabar stop management (stop checked first → conservative)
        if pos is not None:
            if lo <= pos.stop:
                reason = {ProtectionStatus.INITIAL_STOP: "stop_loss", ProtectionStatus.BREAKEVEN_LOCKED:
                          "breakeven_stop", ProtectionStatus.TRAILING: "trailing_stop"}[pos.status]
                close(pos, ts, min(o, pos.stop), reason)
                pos = None
            else:
                ps = PositionState(trade_id=0, symbol=symbol, mint=None, venue="bt", entry_price=pos.entry,
                                   size=pos.size, stop_loss=pos.stop, highest_price=pos.highest, last_price=hi,
                                   protection_status=pos.status.value, initial_stop=pos.initial_stop,
                                   tp1_hit=pos.tp1_hit)
                upd = manage_position(ps, hi, settings)
                pos.stop, pos.status, pos.highest = upd.stop_loss, upd.protection_status, upd.highest_price
                r = pos.entry - pos.initial_stop
                if upd.reason == "take_profit_2":
                    close(pos, ts, max(o, pos.entry + cfg.tp2_r * r), "take_profit_2")   # limit-style fill at TP2
                    pos = None
                elif upd.partial_fraction > 0:
                    q = pos.size * upd.partial_fraction
                    fill = max(o, pos.entry + cfg.tp1_r * r) * (1 - slip)
                    fee_q = fill * q * fee
                    cash += fill * q - fee_q
                    pos.realized_partial += (fill - pos.entry) * q - fee_q
                    pos.size -= q
                    pos.tp1_hit = True

        # 3) signal on this bar's close
        sig, conf, atr = _signals(h1, i, cfg)
        if pos is None and sig == "BUY":
            pending_buy = (max(cfg.atr_mult * atr, c * 0.005), conf)
        elif pos is not None and sig == "SELL" and cfg.technical_exit:
            pending_sell = True

        equity.append((ts, cash + (pos.size * c if pos else 0.0)))

    if pos is not None:
        close(pos, h1.timestamp.iloc[-1], h1.close.iloc[-1], "end_of_test")
        equity[-1] = (equity[-1][0], cash)

    eq = pd.Series(dict(equity))
    bench_px = h1.set_index("timestamp")["close"].loc[eq.index]
    bench = cfg.capital * bench_px / bench_px.iloc[0]
    res = BTResult(symbol, cfg, closed, eq, bench)
    res.metrics = metrics(res)
    return res


def _max_dd(s: pd.Series) -> float:
    return float(((s / s.cummax()) - 1).min() * -100)


def _sharpe(s: pd.Series) -> float:
    daily = s.resample("1D").last().pct_change().dropna()
    return float(daily.mean() / daily.std() * math.sqrt(365)) if daily.std() > 0 else 0.0


def metrics(r: BTResult) -> dict[str, float]:
    pnls = np.array([t.pnl for t in r.trades])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    exposure_h = sum(((t.exit_time - t.entry_time).total_seconds() / 3600) for t in r.trades)
    return {
        "trades": len(pnls),
        "win_rate_pct": float(len(wins) / len(pnls) * 100) if len(pnls) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("inf"),
        "return_pct": float((r.equity.iloc[-1] / r.config.capital - 1) * 100),
        "max_dd_pct": _max_dd(r.equity),
        "sharpe": _sharpe(r.equity),
        "fees": float(sum(t.fees for t in r.trades)),
        "exposure_pct": float(exposure_h / max(1, len(r.equity)) * 100),
        "bench_return_pct": float((r.benchmark.iloc[-1] / r.config.capital - 1) * 100),
        "bench_max_dd_pct": _max_dd(r.benchmark),
        "tp_exits": sum(t.reason == "take_profit_2" for t in r.trades),
        "breakeven_exits": sum(t.reason == "breakeven_stop" for t in r.trades),
        "stop_exits": sum(t.reason == "stop_loss" for t in r.trades),
        "trailing_exits": sum(t.reason == "trailing_stop" for t in r.trades),
        "technical_exits": sum(t.reason == "technical_exit" for t in r.trades),
    }


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--source", choices=["mexc", "geckoterminal"], default="mexc")
    args = ap.parse_args()
    tokens = {"SOL": "So11111111111111111111111111111111111111112", "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"}
    variants = {
        "no targets (old)": BTConfig(),
        "TP1 1.5R 50% + TP2 3R (new default)": BTConfig(tp1_r=1.5, tp2_r=3.0),
        "TP 2R full": BTConfig(tp2_r=2.0),
        "TP1 1R 50% + TP2 2R": BTConfig(tp1_r=1.0, tp2_r=2.0),
    }
    rows = []
    for sym, mint in tokens.items():
        h1 = (await load_mexc(sym, "1h", args.days) if args.source == "mexc"
              else await load(mint, sym, "1h", min(args.days, 180)))
        for name, cfg in variants.items():
            m = run_backtest(sym, h1, cfg).metrics
            rows.append({"symbol": sym, "variant": name, **m})
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250, "display.max_columns", 30)
    print(df.round(2).to_string(index=False))


if __name__ == "__main__":
    asyncio.run(_main())
