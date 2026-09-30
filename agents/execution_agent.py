"""Order execution: paper fills priced from live Jupiter quotes, live Jupiter swaps
(Jito-routed, pre-simulated) or CEX futures via CCXT with exchange-side stops."""
from __future__ import annotations

import uuid
from typing import Literal

from agents.base_agent import BaseAgent
from agents.risk_agent import take_profit_price
from config import Settings, WatchToken
from core.state import ExecutionState, RiskState
from database import repository as repo
from database.models import ProtectionStatus, Trade, TradeDirection, TradeStatus, utcnow
from exchange_connector import ExchangeConnector, MexcSpotConnector, SolanaDEXConnector, walk_book

Venue = Literal["paper", "jupiter", "cex", "mexc"]


class ExecutionAgent(BaseAgent[ExecutionState]):
    name = "execution"

    def __init__(self, settings: Settings, dex: SolanaDEXConnector, cex: ExchangeConnector | None,
                 mexc: MexcSpotConnector | None = None) -> None:
        # Orders are not blindly retried: a timeout after submission could double-fill.
        super().__init__(timeout=120, retries=0)
        self.settings, self.dex, self.cex, self.mexc = settings, dex, cex, mexc

    def venue_for(self, token: WatchToken) -> Venue:
        if not self.settings.is_live:
            return "paper"
        if self.mexc is not None:
            return "mexc"
        if token.cex_symbol and self.cex is not None:
            return "cex"
        return "jupiter"

    # ------------------------------------------------------------------ open
    async def process(self, *, token: WatchToken, risk: RiskState) -> ExecutionState:  # type: ignore[override]
        if not risk.approved:
            raise ValueError("execution requested for a risk-rejected trade")
        venue = self.venue_for(token)
        fee_rate = self.settings.paper_fee_bps / 10_000
        stop_id: str | None = None

        if venue == "paper" and self.mexc is not None:
            # paper fill = a market buy walked through the live MEXC order book
            book = await self.mexc.fetch_ticker(token.symbol)
            price = walk_book(book.asks, usd=risk.notional_usd) if book.asks else book.price
            units = risk.notional_usd / price
            fees, order_id, impact = risk.notional_usd * fee_rate, f"paper-{uuid.uuid4().hex[:12]}", None
        elif venue == "paper":
            _, price, units = await self.dex.quote_usd(token.mint, "BUY", risk.notional_usd)
            fees, order_id, impact = risk.notional_usd * fee_rate, f"paper-{uuid.uuid4().hex[:12]}", None
        elif venue == "mexc":
            assert self.mexc is not None
            fill = await self.mexc.market_buy_usd(token.symbol, risk.notional_usd)
            price, units, fees, order_id, impact = fill["price"], fill["units"], fill["fee_usd"], fill["order_id"], None
        elif venue == "jupiter":
            fill = await self.dex.execute_swap(token.mint, "BUY", risk.notional_usd)
            price, units, order_id = fill["price"], fill["units"], fill["signature"]
            fees, impact = 0.0, fill["price_impact_pct"]
        else:
            assert self.cex is not None and token.cex_symbol
            entry, stop = await self.cex.create_order_with_stop(token.cex_symbol, "buy", risk.position_size,
                                                                risk.stop_loss)
            price = float(entry.get("average") or entry.get("price") or risk.entry_price)
            units = float(entry.get("filled") or risk.position_size)
            fees = float((entry.get("fee") or {}).get("cost") or 0.0)
            order_id, stop_id, impact = str(entry["id"]), str(stop.get("id")), None

        # Re-anchor the stop to the actual fill so the $-risk stays what the risk agent approved.
        stop_loss = min(risk.stop_loss, price - (risk.entry_price - risk.stop_loss))
        trade = await repo.save_trade(Trade(
            symbol=token.symbol, mint=token.mint, venue=venue, direction=TradeDirection.LONG,
            entry_price=price, size=units, stop_loss=stop_loss, initial_stop=stop_loss,
            risk_usd=units * (price - stop_loss), status=TradeStatus.OPEN, pnl=0.0,
            protection_status=ProtectionStatus.INITIAL_STOP, highest_price=price, last_price=price,
            fees_usd=fees, entry_order_id=order_id, stop_order_id=stop_id,
            tp1=take_profit_price(price, stop_loss, self.settings.tp1_r),
            tp2=take_profit_price(price, stop_loss, self.settings.tp2_r)))
        self.log.success("OPEN #{} {} {:.6f} @ {:.6f} stop {:.6f} [{}]", trade.id, token.symbol, units, price,
                         stop_loss, venue)
        return ExecutionState(symbol=token.symbol, side="BUY", venue=venue, status="FILLED", order_id=order_id,
                              executed_price=price, size=units, fees_usd=fees, price_impact_pct=impact)

    # ------------------------------------------------------------------ close
    async def _sell(self, trade: Trade, units: float, mark_price: float | None,
                    final: bool) -> tuple[float, float, float, str]:
        """Sell `units` of a trade on its venue. Returns (avg price, gross USD, fees USD, order id)."""
        venue: Venue = trade.venue  # type: ignore[assignment]
        fee_rate = self.settings.paper_fee_bps / 10_000
        if venue == "paper":
            try:
                if self.mexc is not None:
                    book = await self.mexc.fetch_ticker(trade.symbol)
                    price = walk_book(book.bids, units=units) if book.bids else book.price
                else:
                    _, price, _ = await self.dex.quote_usd(trade.mint or "", "SELL", units)
            except Exception as exc:
                if mark_price is None:
                    raise
                self.log.warning("paper exit quote failed ({}); using mark {}", exc, mark_price)
                price = mark_price
            usd = price * units
            return price, usd, usd * fee_rate, f"paper-{uuid.uuid4().hex[:12]}"
        if venue == "mexc":
            if self.mexc is None:
                raise RuntimeError("MEXC trade open but MEXC connector unavailable")
            fill = await self.mexc.market_sell(trade.symbol, units)
            return fill["price"], fill["usd"], fill["fee_usd"], fill["order_id"]
        if venue == "jupiter":
            fill = await self.dex.execute_swap(trade.mint or "", "SELL", units)
            return fill["price"], fill["usd"], 0.0, fill["signature"]
        if self.cex is None:
            raise RuntimeError("CEX trade open but CEX connector unavailable")
        symbol = next((t.cex_symbol for t in self.settings.watchlist if t.symbol == trade.symbol), None)
        if not symbol:
            raise RuntimeError(f"no CEX symbol for {trade.symbol}")
        if final:
            order = await self.cex.close_position(symbol, units, trade.stop_order_id)
        else:
            order = await self.cex.reduce_position(symbol, units)
            stop = await self.cex.replace_stop(symbol, trade.stop_order_id, trade.size - units, trade.stop_loss)
            trade.stop_order_id = str(stop.get("id"))
        price = float(order.get("average") or order.get("price") or mark_price or trade.last_price)
        return price, price * units, float((order.get("fee") or {}).get("cost") or 0.0), str(order["id"])

    async def close_trade(self, trade: Trade, reason: str, mark_price: float | None = None) -> ExecutionState:
        price, usd, fees, order_id = await self._sell(trade, trade.size, mark_price, final=True)
        trade.fees_usd += fees
        # realized_partial already contains profit (net of its fees) from earlier partial exits
        trade.pnl = usd - trade.entry_price * trade.size - trade.fees_usd + (trade.realized_partial or 0.0)
        trade.exit_price, trade.last_price = price, price
        trade.status, trade.closed_at, trade.exit_reason, trade.exit_order_id = TradeStatus.CLOSED, utcnow(), reason, order_id
        await repo.save_trade(trade)
        self.log.info("CLOSE #{} {} @ {:.6f} pnl {:+.2f} USD ({})", trade.id, trade.symbol, price, trade.pnl, reason)
        return ExecutionState(symbol=trade.symbol, side="SELL", venue=trade.venue, status="FILLED",  # type: ignore[arg-type]
                              order_id=order_id, executed_price=price, size=trade.size, fees_usd=fees, pnl=trade.pnl)

    async def partial_close(self, trade: Trade, fraction: float, reason: str,
                            mark_price: float | None = None) -> ExecutionState:
        """Take profit on `fraction` of the position (TP1); the rest keeps running."""
        units = trade.size * fraction
        price, usd, fees, order_id = await self._sell(trade, units, mark_price, final=False)
        gain = usd - trade.entry_price * units - fees
        trade.realized_partial = (trade.realized_partial or 0.0) + gain
        trade.size -= units
        trade.tp1_hit, trade.last_price = True, price
        await repo.save_trade(trade)
        self.log.success("TP1 #{} {} sold {:.6f} @ {:.6f} (+{:.2f} USD), {:.6f} left", trade.id, trade.symbol, units,
                         price, gain, trade.size)
        return ExecutionState(symbol=trade.symbol, side="SELL", venue=trade.venue, status="FILLED",  # type: ignore[arg-type]
                              order_id=order_id, executed_price=price, size=units, fees_usd=fees, pnl=gain)

    async def sync_stop(self, trade: Trade, new_stop: float) -> None:
        """Move the exchange-side stop (CEX). DEX/paper stops are enforced by the guard loop."""
        if trade.venue == "cex" and self.cex is not None:
            symbol = next((t.cex_symbol for t in self.settings.watchlist if t.symbol == trade.symbol), None)
            if symbol:
                order = await self.cex.replace_stop(symbol, trade.stop_order_id, trade.size, new_stop)
                trade.stop_order_id = str(order.get("id"))
