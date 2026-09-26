"""Query helpers shared by the engine and the dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from loguru import logger
from sqlalchemy import desc, func, select

from database.connection import get_session
from database.models import (AgentDecision, AgentHeartbeat, ControlState, MarketTick,
                             PortfolioSnapshot, SystemLog, Trade, TradeStatus, WhaleTransaction,
                             utcnow)


async def get_control(default_risk_pct: float, default_kelly: float) -> ControlState:
    async with get_session() as s:
        row = await s.get(ControlState, 1)
        if row is None:
            row = ControlState(id=1, halted=False, close_all_requested=False,
                               risk_pct=default_risk_pct, kelly_fraction=default_kelly)
            s.add(row)
        return row


async def update_control(**fields: Any) -> None:
    async with get_session() as s:
        row = await s.get(ControlState, 1)
        if row is None:
            raise ValueError("control_state not initialised; start the engine once first")
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utcnow()


async def heartbeat(agent: str, status: str, latency_ms: float | None = None,
                    error: str | None = None, detail: str | None = None) -> None:
    async with get_session() as s:
        row = await s.get(AgentHeartbeat, agent)
        if row is None:
            row = AgentHeartbeat(agent=agent, status=status)
            s.add(row)
        row.status, row.last_seen, row.last_latency_ms = status, utcnow(), latency_ms
        row.last_error = error
        if detail is not None:
            row.detail = detail


async def record(*objects: Any) -> None:
    async with get_session() as s:
        s.add_all(objects)


async def save_trade(trade: Trade) -> Trade:
    async with get_session() as s:
        merged = await s.merge(trade)
        await s.flush()
        return merged


async def open_trades() -> list[Trade]:
    async with get_session() as s:
        res = await s.execute(select(Trade).where(Trade.status == TradeStatus.OPEN).order_by(Trade.id))
        return list(res.scalars())


async def closed_trades(limit: int = 500) -> list[Trade]:
    async with get_session() as s:
        res = await s.execute(select(Trade).where(Trade.status == TradeStatus.CLOSED)
                              .order_by(desc(Trade.closed_at)).limit(limit))
        return list(res.scalars())


async def realized_pnl_total() -> float:
    async with get_session() as s:
        return float((await s.execute(select(func.coalesce(func.sum(Trade.pnl), 0.0))
                                      .where(Trade.status == TradeStatus.CLOSED))).scalar_one())


async def realized_pnl_since(since: datetime) -> float:
    async with get_session() as s:
        return float((await s.execute(select(func.coalesce(func.sum(Trade.pnl), 0.0))
                                      .where(Trade.status == TradeStatus.CLOSED,
                                             Trade.closed_at >= since))).scalar_one())


async def peak_equity() -> float | None:
    async with get_session() as s:
        return (await s.execute(select(func.max(PortfolioSnapshot.equity_usd)))).scalar()


async def recent(model: type, limit: int = 100, since: timedelta | None = None) -> Sequence[Any]:
    async with get_session() as s:
        q = select(model).order_by(desc(model.timestamp)).limit(limit)
        if since is not None:
            q = q.where(model.timestamp >= datetime.now(timezone.utc) - since)
        return list((await s.execute(q)).scalars())


async def heartbeats() -> list[AgentHeartbeat]:
    async with get_session() as s:
        return list((await s.execute(select(AgentHeartbeat).order_by(AgentHeartbeat.agent))).scalars())


async def db_log_sink(message: Any) -> None:
    """loguru async sink: persists WARNING+ records to system_logs."""
    rec = message.record
    try:
        await record(SystemLog(timestamp=rec["time"].astimezone(timezone.utc), level=rec["level"].name,
                               message=rec["message"][:4000], module=f'{rec["name"]}:{rec["function"]}'))
    except Exception as exc:  # never let logging crash the engine
        print(f"[db_log_sink] failed to persist log: {exc}")


__all__ = ["get_control", "update_control", "heartbeat", "record", "save_trade", "open_trades",
           "closed_trades", "realized_pnl_total", "realized_pnl_since", "peak_equity", "recent",
           "heartbeats", "db_log_sink", "MarketTick", "WhaleTransaction", "AgentDecision", "logger"]
