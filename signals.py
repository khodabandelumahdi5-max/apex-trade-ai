"""Print the current trade plan for every watchlist token: signal, entry, stop, targets and size.

    python signals.py

Read-only: places no orders and needs no API keys. Uses the same technical agent and risk rules as
the engine, with EXECUTION_VENUE deciding where prices come from (MEXC or Jupiter/GeckoTerminal)."""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from agents.risk_agent import HARD_MAX_RISK_PCT, PRIOR_PAYOFF, PRIOR_WIN_RATE, EdgeEstimate
from agents.technical_agent import TechnicalAgent
from config import get_settings
from core.market_data import GeckoTerminalClient
from core.state import MarketDataState
from exchange_connector import MexcSpotConnector, SolanaDEXConnector, SolanaRPC


def _fmt(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}" if x >= 1 else f"{x:.6f}"


async def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    s = get_settings()
    rpc = SolanaRPC(s.solana_rpc_url)
    dex = SolanaDEXConnector(s, rpc)
    candles = GeckoTerminalClient(s.geckoterminal_base)
    mexc = MexcSpotConnector(s) if s.execution_venue == "mexc" else None
    tech = TechnicalAgent(s, candles, dex, None, mexc)
    kelly_risk = s.kelly_fraction * EdgeEstimate(PRIOR_WIN_RATE, PRIOR_PAYOFF, 0).kelly * 100
    risk_pct = min(s.max_risk_per_trade_pct, HARD_MAX_RISK_PCT, kelly_risk)
    budget = s.initial_capital_usd
    print(f"\nTrade plan · source: {s.execution_venue.upper()} · budget ${budget:,.0f} · "
          f"risk/trade {risk_pct:.2f}% · TP1 {s.tp1_r}R (sell {s.tp1_fraction:.0%}) · TP2 {s.tp2_r}R\n")
    try:
        prices = (await mexc.last_prices([t.symbol for t in s.watchlist]) if mexc
                  else {t.symbol: float(v["usdPrice"]) for t in s.watchlist
                        for m, v in (await dex.get_prices([t.mint])).items()})
        for token in s.watchlist:
            try:
                market = (await mexc.fetch_ticker(token.symbol) if mexc else
                          MarketDataState(symbol=token.symbol, mint=token.mint, price=prices[token.symbol]))
                t = await tech.process(token=token, market=market)
            except Exception as exc:
                print(f"{token.symbol:<6} error: {exc}\n")
                continue
            entry = market.ask or market.price
            stop = t.suggested_stop
            print(f"{token.symbol:<6} price {_fmt(market.price)}   signal {t.signal:<4} confidence {t.confidence:.2f}"
                  f"   RSI(1h) {t.rsi_14:.0f}   order-book imbalance {t.order_book_imbalance:+.2f}")
            if stop:
                r = entry - stop
                size = min(budget * risk_pct / 100 / r, budget * s.max_position_pct / 100 / entry)
                print(f"       entry ≈ {_fmt(entry)}   stop {_fmt(stop)} (−{r / entry:.1%})   "
                      f"TP1 {_fmt(t.tp1)} (+{(t.tp1 or entry) / entry - 1:.1%})   "
                      f"TP2 {_fmt(t.tp2)} (+{(t.tp2 or entry) / entry - 1:.1%})")
                print(f"       size {size:,.4f} {token.symbol} ≈ ${size * entry:,.2f}   "
                      f"max loss ≈ ${size * r:,.2f}   breakeven after +{s.breakeven_trigger_pct}%")
            verdict = ("the technical agent would BUY (the engine also needs the on-chain agent's consent)"
                       if t.signal == "BUY" else "no entry now" if t.signal == "HOLD"
                       else "trend broken: the engine would exit an open position")
            print(f"       → {verdict}\n         {'; '.join(t.reasons)}\n")
    finally:
        await dex.close(); await candles.close(); await rpc.close()
        if mexc:
            await mexc.close()
    print("Not financial advice. The 6-month backtest of this strategy was negative; see README → Backtest.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
