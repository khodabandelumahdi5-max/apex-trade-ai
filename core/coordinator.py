"""Swarm orchestrator: OnChain ∥ Technical → consensus gate → Risk → Execution,
plus a fast guard loop enforcing stops and the Zero-Loss Breakeven Protocol."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import select

from agents.base_agent import AgentError
from agents.execution_agent import ExecutionAgent
from agents.onchain_agent import OnChainAgent
from agents.risk_agent import RiskAgent, manage_position, portfolio_var
from agents.technical_agent import TechnicalAgent
from config import Settings, WatchToken
from core.event_bus import EventBus
from core.market_data import GeckoTerminalClient
from core.state import (AgentHealth, GlobalPortfolioState, MarketDataState, OnChainState, PositionState,
                        TechnicalState)
from database import repository as repo
from database.connection import get_session
from database.models import (AgentDecision, MarketTick, PortfolioSnapshot, ProtectionStatus, Trade,
                             TradeStatus)
from exchange_connector import ExchangeConnector, SolanaDEXConnector, SolanaRPC

GUARD_INTERVAL_SEC = 5.0
AUTO_HALT_PREFIX = "auto:"        # halts the engine may lift itself (connectivity); others need a human
AUTO_RESUME_AFTER_OK_CYCLES = 3


class SwarmCoordinator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.rpc = SolanaRPC(settings.solana_rpc_url)
        self.dex = SolanaDEXConnector(settings, self.rpc)
        self.candles = GeckoTerminalClient(settings.geckoterminal_base)
        self.cex: ExchangeConnector | None = ExchangeConnector(settings) if settings.cex_exchange else None
        self.onchain = OnChainAgent(settings, self.rpc)
        self.technical = TechnicalAgent(settings, self.candles, self.dex, self.cex)
        self.risk = RiskAgent(settings)
        self.execution = ExecutionAgent(settings, self.dex, self.cex)
        self._trade_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._consecutive_failures = 0
        self._consecutive_ok = 0
        self.bus.subscribe("*", self._log_event)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await repo.get_control(self.settings.max_risk_per_trade_pct, self.settings.kelly_fraction)
        if self.cex is not None:
            try:
                await self.cex.connect()
            except Exception as exc:
                logger.error("CEX unavailable ({}); continuing DEX-only", exc)
                await self.cex.close()
                self.cex = self.technical.cex = self.execution.cex = None
        for agent in (self.onchain, self.technical, self.risk, self.execution):
            await agent.startup()
            await agent.set_health(AgentHealth.HEALTHY, "started")
        logger.info("Swarm online: mode={} watchlist={} onchain_mode={}", self.settings.trading_mode,
                    [t.symbol for t in self.settings.watchlist], self.settings.onchain_mode)

    async def run_forever(self) -> None:
        guard = asyncio.create_task(self._guard_loop(), name="guard")
        try:
            while not self._stop.is_set():
                cycle = asyncio.create_task(self.cycle(), name="cycle")
                stopper = asyncio.create_task(self._stop.wait())
                await asyncio.wait({cycle, stopper}, return_when=asyncio.FIRST_COMPLETED)
                stopper.cancel()
                if not cycle.done():
                    logger.info("stop requested: cancelling in-flight cycle")
                    cycle.cancel()
                    await asyncio.gather(cycle, return_exceptions=True)
                    break
                try:
                    cycle.result()
                    self._consecutive_failures = 0
                    self._consecutive_ok += 1
                    await self._maybe_auto_resume()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._consecutive_failures += 1
                    self._consecutive_ok = 0
                    logger.exception("cycle failed ({}/{}): {!r}", self._consecutive_failures,
                                     self.settings.max_consecutive_failures, exc)
                    if self._consecutive_failures == self.settings.max_consecutive_failures:
                        await self.trip_circuit_breaker(
                            f"{AUTO_HALT_PREFIX} {self._consecutive_failures} consecutive cycle failures "
                            f"(network/API); resumes automatically after {AUTO_RESUME_AFTER_OK_CYCLES} good cycles")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.settings.loop_interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        for agent in (self.onchain, self.technical, self.risk, self.execution):
            try:
                await agent.shutdown()
                await agent.set_health(AgentHealth.STOPPED, "shutdown")
            except Exception as exc:
                logger.warning("{} shutdown error: {}", agent.name, exc)
        for closer in (self.dex.close, self.candles.close, self.rpc.close):
            await closer()
        if self.cex is not None:
            await self.cex.close()

    async def _maybe_auto_resume(self) -> None:
        """Lift a connectivity halt once cycles succeed again. Risk halts (drawdown, dashboard
        emergency halt) never auto-resume."""
        if self._consecutive_ok < AUTO_RESUME_AFTER_OK_CYCLES:
            return
        control = await repo.get_control(self.settings.max_risk_per_trade_pct, self.settings.kelly_fraction)
        reason = control.reason or ""
        # also matches halts written by older versions ("21 consecutive cycle failures")
        auto = reason.startswith(AUTO_HALT_PREFIX) or reason.endswith("consecutive cycle failures")
        if control.halted and auto and not control.close_all_requested:
            await repo.update_control(halted=False, reason=None)
            logger.warning("connectivity restored ({} good cycles) → auto-resuming", self._consecutive_ok)
            await self.bus.publish("resume", {"reason": "connectivity restored"})

    async def trip_circuit_breaker(self, reason: str) -> None:
        logger.critical("CIRCUIT BREAKER: {} → halting new entries", reason)
        await repo.update_control(halted=True, reason=reason)
        await self.bus.publish("halt", {"reason": reason})

    # ------------------------------------------------------------------ portfolio
    async def portfolio(self, prices: dict[str, float], halted: bool) -> GlobalPortfolioState:
        trades = await repo.open_trades()
        realized = await repo.realized_pnl_total()
        positions = [PositionState(
            trade_id=t.id, symbol=t.symbol, mint=t.mint, venue=t.venue, entry_price=t.entry_price, size=t.size,
            stop_loss=t.stop_loss, highest_price=t.highest_price,
            last_price=prices.get(t.mint or "", t.last_price or t.entry_price),
            protection_status=t.protection_status.value) for t in trades]
        cost = sum(t.entry_price * t.size + t.fees_usd for t in trades)
        cash = self.settings.initial_capital_usd + realized - cost
        unrealized = sum(p.unrealized_pnl for p in positions)
        equity = cash + sum(p.size * p.last_price for p in positions)
        peak = max(await repo.peak_equity() or 0.0, equity, self.settings.initial_capital_usd)
        exposures = {p.symbol: p.size * p.last_price for p in positions}
        var_usd = portfolio_var(exposures, await self._hourly_returns(), self.settings.var_confidence)
        return GlobalPortfolioState(
            total_capital=equity, cash=cash, realized_pnl=realized, unrealized_pnl=unrealized,
            active_trades_count=len(positions),
            protected_trades_count=sum(p.protection_status != ProtectionStatus.INITIAL_STOP.value
                                       for p in positions),
            portfolio_var=var_usd, portfolio_var_pct=var_usd / equity * 100 if equity > 0 else 0.0,
            peak_equity=peak, current_drawdown=max(0.0, min(100.0, (1 - equity / peak) * 100)),
            halted=halted, positions=positions)

    async def _hourly_returns(self) -> dict[str, pd.Series]:
        out: dict[str, pd.Series] = {}
        for token in self.settings.watchlist:
            try:
                df = await self.candles.ohlcv(token.mint, "1h", limit=720)
                out[token.symbol] = np.log(df.set_index("timestamp")["close"]).diff().dropna()
            except Exception as exc:
                logger.debug("returns for {} unavailable: {}", token.symbol, exc)
        return out

    async def _daily_start_equity(self, fallback: float) -> float:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        async with get_session() as s:
            row = (await s.execute(select(PortfolioSnapshot.equity_usd)
                                   .where(PortfolioSnapshot.timestamp >= midnight)
                                   .order_by(PortfolioSnapshot.timestamp).limit(1))).scalar()
        return float(row) if row else fallback

    # ------------------------------------------------------------------ market data
    async def _prices(self) -> dict[str, dict[str, Any]]:
        return await self.dex.get_prices([t.mint for t in self.settings.watchlist])

    async def _market_state(self, token: WatchToken, px: dict[str, Any]) -> MarketDataState:
        if self.cex is not None and token.cex_symbol:
            try:
                state = await self.cex.fetch_ticker(token.cex_symbol)
                return state.model_copy(update={"symbol": token.symbol, "mint": token.mint,
                                                "price": float(px["usdPrice"]),
                                                "liquidity_usd": px.get("liquidity")})
            except Exception as exc:
                logger.warning("CEX book for {} unavailable: {}", token.symbol, exc)
        return MarketDataState(symbol=token.symbol, mint=token.mint, price=float(px["usdPrice"]),
                               liquidity_usd=px.get("liquidity"),
                               price_change_24h_pct=px.get("priceChange24h"), source="jupiter")

    # ------------------------------------------------------------------ main cycle
    async def cycle(self) -> None:
        control = await repo.get_control(self.settings.max_risk_per_trade_pct, self.settings.kelly_fraction)
        if control.close_all_requested:
            await self.close_all("emergency_halt")
            await repo.update_control(close_all_requested=False, halted=True)
            control.halted = True

        prices = await self._prices()
        mark = {m: float(v["usdPrice"]) for m, v in prices.items()}
        missing = [t.symbol for t in self.settings.watchlist if t.mint not in prices]
        if missing:
            logger.warning("no Jupiter price for {}", missing)

        for token in self.settings.watchlist:
            if token.mint not in prices:
                continue
            market = await self._market_state(token, prices[token.mint])
            await repo.record(MarketTick(symbol=token.symbol, price=market.price, bid=market.bid, ask=market.ask,
                                         order_book_imbalance=market.order_book_imbalance,
                                         source=market.source))
            await self._evaluate(token, market, control.halted, control.risk_pct, control.kelly_fraction, mark)

        pf = await self.portfolio(mark, control.halted)
        await repo.record(PortfolioSnapshot(equity_usd=pf.total_capital, cash_usd=pf.cash, var_usd=pf.portfolio_var,
                                            drawdown_pct=pf.current_drawdown, open_positions=pf.active_trades_count))
        if pf.current_drawdown >= self.settings.max_drawdown_pct and not control.halted:
            await self.trip_circuit_breaker(f"drawdown {pf.current_drawdown:.2f}% ≥ {self.settings.max_drawdown_pct}%")
        logger.info("equity ${:,.2f} | cash ${:,.2f} | open {} | VaR ${:,.2f} | DD {:.2f}%",
                    pf.total_capital, pf.cash, pf.active_trades_count, pf.portfolio_var, pf.current_drawdown)

    async def _evaluate(self, token: WatchToken, market: MarketDataState, halted: bool, risk_pct: float,
                        kelly_fraction: float, mark: dict[str, float]) -> None:
        onchain_res, tech_res = await asyncio.gather(
            self.onchain.run(token=token, price=market.price),
            self.technical.run(token=token, market=market), return_exceptions=True)
        onchain = onchain_res if isinstance(onchain_res, OnChainState) else None
        tech = tech_res if isinstance(tech_res, TechnicalState) else None
        for agent, res in (("onchain", onchain_res), ("technical", tech_res)):
            if isinstance(res, BaseException):
                logger.error("{} agent failed for {}: {}", agent, token.symbol, res)

        await repo.record(*[d for d in (
            AgentDecision(agent="onchain", symbol=token.symbol, decision=onchain.whale_sentiment,
                          confidence=onchain.confidence, payload=onchain.model_dump(mode="json")) if onchain else None,
            AgentDecision(agent="technical", symbol=token.symbol, decision=tech.signal, confidence=tech.confidence,
                          payload=tech.model_dump(mode="json")) if tech else None) if d is not None])

        open_for_symbol = [t for t in await repo.open_trades() if t.symbol == token.symbol]
        if open_for_symbol and tech and tech.signal == "SELL":
            async with self._trade_lock:
                for trade in open_for_symbol:
                    await self._close(trade, "technical_exit", market.price)
            return
        if halted or open_for_symbol or onchain is None or tech is None:
            return

        onchain_ok = (onchain.whale_sentiment == "ACCUMULATION" if self.settings.onchain_mode == "strict"
                      else onchain.whale_sentiment != "DISTRIBUTION")
        consensus = tech.signal == "BUY" and onchain_ok
        await self.bus.publish("consensus", {"symbol": token.symbol, "technical": tech.signal,
                                             "tech_conf": round(tech.confidence, 3),
                                             "onchain": onchain.whale_sentiment, "consensus": consensus})
        if not consensus:
            return

        pf = await self.portfolio(mark, halted)
        try:
            risk = await self.risk.run(symbol=token.symbol, entry_price=market.price, technical=tech, portfolio=pf,
                                       closed=await repo.closed_trades(), hourly_returns=await self._hourly_returns(),
                                       risk_pct=risk_pct, kelly_fraction=kelly_fraction,
                                       daily_start_equity=await self._daily_start_equity(pf.total_capital))
        except AgentError as exc:
            logger.error("risk agent failed for {}: {}", token.symbol, exc)
            return
        await repo.record(AgentDecision(agent="risk", symbol=token.symbol,
                                        decision="APPROVED" if risk.approved else "REJECTED",
                                        confidence=None, payload=risk.model_dump(mode="json")))
        if not risk.approved:
            logger.info("risk rejected {}: {}", token.symbol, "; ".join(risk.reasons))
            return
        async with self._trade_lock:
            try:
                result = await self.execution.run(token=token, risk=risk)
            except AgentError as exc:
                logger.error("execution failed for {}: {}", token.symbol, exc)
                return
        await repo.record(AgentDecision(agent="execution", symbol=token.symbol, decision=result.status,
                                        confidence=None, payload=result.model_dump(mode="json")))
        await self.bus.publish("trade_opened", result.model_dump(mode="json"))

    # ------------------------------------------------------------------ guard loop
    async def _guard_loop(self) -> None:
        """Every few seconds: mark open trades, apply breakeven/trailing stops, exit on stop hits,
        and react to the dashboard's emergency halt without waiting for the next analysis cycle."""
        while True:
            try:
                control = await repo.get_control(self.settings.max_risk_per_trade_pct, self.settings.kelly_fraction)
                if control.close_all_requested:
                    await self.close_all("emergency_halt")
                    await repo.update_control(close_all_requested=False, halted=True)
                trades = await repo.open_trades()
                if trades:
                    mints = list({t.mint for t in trades if t.mint})
                    prices = await self.dex.get_prices(mints)
                    for trade in trades:
                        px = prices.get(trade.mint or "")
                        if px:
                            await self.guard_trade(trade, float(px["usdPrice"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("guard loop error: {!r}", exc)
            await asyncio.sleep(GUARD_INTERVAL_SEC)

    async def guard_trade(self, trade: Trade, price: float) -> None:
        pos = PositionState(trade_id=trade.id, symbol=trade.symbol, mint=trade.mint, venue=trade.venue,
                            entry_price=trade.entry_price, size=trade.size, stop_loss=trade.stop_loss,
                            highest_price=trade.highest_price, last_price=price,
                            protection_status=trade.protection_status.value)
        upd = manage_position(pos, price, self.settings)
        async with self._trade_lock:
            async with get_session() as s:
                current = await s.get(Trade, trade.id)
                if current is None or current.status != TradeStatus.OPEN:
                    return
            if upd.exit:
                await self._close(trade, upd.reason or "stop", price)
                return
            moved = upd.stop_loss != trade.stop_loss
            if moved:
                await self.execution.sync_stop(trade, upd.stop_loss)
            became_breakeven = (trade.protection_status == ProtectionStatus.INITIAL_STOP
                                and upd.protection_status != ProtectionStatus.INITIAL_STOP)
            trade.stop_loss, trade.protection_status = upd.stop_loss, upd.protection_status
            trade.highest_price, trade.last_price = upd.highest_price, price
            trade.pnl = (price - trade.entry_price) * trade.size - trade.fees_usd
            await repo.save_trade(trade)
        if became_breakeven:
            await self.bus.publish("breakeven_locked", {"trade_id": trade.id, "symbol": trade.symbol,
                                                        "stop": trade.stop_loss, "price": price})

    async def _close(self, trade: Trade, reason: str, price: float) -> None:
        async with get_session() as s:  # re-check under lock: another path may have closed it
            current = await s.get(Trade, trade.id)
            if current is None or current.status != TradeStatus.OPEN:
                return
        try:
            result = await self.execution.close_trade(trade, reason, mark_price=price)
        except Exception as exc:
            logger.critical("FAILED TO CLOSE trade #{} {} ({}): {!r}", trade.id, trade.symbol, reason, exc)
            return
        await self.bus.publish("trade_closed", result.model_dump(mode="json") | {"reason": reason})

    async def close_all(self, reason: str) -> None:
        trades = await repo.open_trades()
        logger.warning("closing ALL {} open positions ({})", len(trades), reason)
        prices = await self.dex.get_prices([t.mint for t in trades if t.mint]) if trades else {}
        async with self._trade_lock:
            for trade in trades:
                mark = float(prices.get(trade.mint or "", {}).get("usdPrice") or trade.last_price or trade.entry_price)
                await self._close(trade, reason, mark)

    @staticmethod
    async def _log_event(topic: str, payload: dict[str, Any]) -> None:
        logger.info("event {} {}", topic, payload)
