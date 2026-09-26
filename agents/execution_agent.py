"""Order execution: paper fills priced from live Jupiter quotes, live Jupiter swaps
(Jito-routed, pre-simulated) or CEX futures via CCXT with exchange-side stops."""
from __future__ import annotations

import uuid
from typing import Literal

from agents.base_agent import BaseAgent
from config import Settings, WatchToken
from core.state import ExecutionState, RiskState
from database import repository as repo
from database.models import ProtectionStatus, Trade, TradeDirection, TradeStatus, utcnow
from exchange_connector import ExchangeConnector, SolanaDEXConnector

Venue = Literal["paper", "jupiter", "cex"]


class ExecutionAgent(BaseAgent[ExecutionState]):
    name = "execution"

    def __init__(self, settings: Settings, dex: SolanaDEXConnector, cex: ExchangeConnector | None) -> None:
        # Orders are not blindly retried: a timeout after submission could double-fill.
        super().__init__(timeout=120, retries=0)
        self.settings, self.dex, self.cex = settings, dex, cex

    def venue_for(self, token: WatchToken) -> Venue:
        if not self.settings.is_live:
            return "paper"
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

        if venue == "paper":
            _, price, units = await self.dex.quote_usd(token.mint, "BUY", risk.notional_usd)
            fees, order_id, impact = risk.notional_usd * fee_rate, f"paper-{uuid.uuid4().hex[:12]}", None
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
            fees_usd=fees, entry_order_id=order_id, stop_order_id=stop_id))
        self.log.success("OPEN #{} {} {:.6f} @ {:.6f} stop {:.6f} [{}]", trade.id, token.symbol, units, price,
                         stop_loss, venue)
        return ExecutionState(symbol=token.symbol, side="BUY", venue=venue, status="FILLED", order_id=order_id,
                              executed_price=price, size=units, fees_usd=fees, price_impact_pct=impact)

    # ------------------------------------------------------------------ close
    async def close_trade(self, trade: Trade, reason: str, mark_price: float | None = None) -> ExecutionState:
        venue: Venue = trade.venue  # type: ignore[assignment]
        fee_rate = self.settings.paper_fee_bps / 10_000
        if venue == "paper":
            try:
                _, price, usd = await self.dex.quote_usd(trade.mint or "", "SELL", trade.size)
            except Exception as exc:
                if mark_price is None:
                    raise
                self.log.warning("paper exit quote failed ({}); using mark {}", exc, mark_price)
                price, usd = mark_price, mark_price * trade.size
            fees, order_id = usd * fee_rate, f"paper-{uuid.uuid4().hex[:12]}"
        elif venue == "jupiter":
            fill = await self.dex.execute_swap(trade.mint or "", "SELL", trade.size)
            price, usd, fees, order_id = fill["price"], fill["usd"], 0.0, fill["signature"]
        else:
            if self.cex is None:
                raise RuntimeError("CEX trade open but CEX connector unavailable")
            symbol = next((t.cex_symbol for t in self.settings.watchlist if t.symbol == trade.symbol), None)
            if not symbol:
                raise RuntimeError(f"no CEX symbol for {trade.symbol}")
            order = await self.cex.close_position(symbol, trade.size, trade.stop_order_id)
            price = float(order.get("average") or order.get("price") or mark_price or trade.last_price)
            usd, fees, order_id = price * trade.size, float((order.get("fee") or {}).get("cost") or 0.0), str(order["id"])

        trade.fees_usd += fees
        trade.pnl = usd - trade.entry_price * trade.size - trade.fees_usd
        trade.exit_price, trade.last_price = price, price
        trade.status, trade.closed_at, trade.exit_reason, trade.exit_order_id = TradeStatus.CLOSED, utcnow(), reason, order_id
        await repo.save_trade(trade)
        self.log.info("CLOSE #{} {} @ {:.6f} pnl {:+.2f} USD ({})", trade.id, trade.symbol, price, trade.pnl, reason)
        return ExecutionState(symbol=trade.symbol, side="SELL", venue=venue, status="FILLED", order_id=order_id,
                              executed_price=price, size=trade.size, fees_usd=fees, pnl=trade.pnl)

    async def sync_stop(self, trade: Trade, new_stop: float) -> None:
        """Move the exchange-side stop (CEX). DEX/paper stops are enforced by the guard loop."""
        if trade.venue == "cex" and self.cex is not None:
            symbol = next((t.cex_symbol for t in self.settings.watchlist if t.symbol == trade.symbol), None)
            if symbol:
                order = await self.cex.replace_stop(symbol, trade.stop_order_id, trade.size, new_stop)
                trade.stop_order_id = str(order.get("id"))
