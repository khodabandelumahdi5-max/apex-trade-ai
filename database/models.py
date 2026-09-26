"""SQLAlchemy 2.0 ORM models (PostgreSQL / TimescaleDB, SQLite for local dev)."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Enum, Float, Index, Integer,
                        String, Text)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# BigInteger on Postgres, INTEGER (rowid alias → autoincrement) on SQLite
PK = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TradeDirection(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class ProtectionStatus(str, enum.Enum):
    INITIAL_STOP = "INITIAL_STOP"
    BREAKEVEN_LOCKED = "BREAKEVEN_LOCKED"
    TRAILING = "TRAILING"


class MarketTick(Base):
    """Time-series table (TimescaleDB hypertable on `timestamp`)."""

    __tablename__ = "market_ticks"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float | None] = mapped_column(Float)
    bid: Mapped[float | None] = mapped_column(Float)
    ask: Mapped[float | None] = mapped_column(Float)
    order_book_imbalance: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="jupiter", nullable=False)

    __table_args__ = (Index("ix_market_ticks_symbol_ts", "symbol", "timestamp"),)


class WhaleTransaction(Base):
    """Time-series table (TimescaleDB hypertable on `timestamp`)."""

    __tablename__ = "whale_transactions"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    token: Mapped[str] = mapped_column(String(32), nullable=False)
    net_flow_usd: Mapped[float] = mapped_column(Float, nullable=False)
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    wallet: Mapped[str | None] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(String(128))
    method: Mapped[str] = mapped_column(String(32), default="holders_delta", nullable=False)

    __table_args__ = (Index("ix_whale_tx_token_ts", "token", "timestamp"),)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[TradeDirection] = mapped_column(Enum(TradeDirection, native_enum=False), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[TradeStatus] = mapped_column(Enum(TradeStatus, native_enum=False), nullable=False, index=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    protection_status: Mapped[ProtectionStatus] = mapped_column(
        Enum(ProtectionStatus, native_enum=False), default=ProtectionStatus.INITIAL_STOP, nullable=False)

    venue: Mapped[str] = mapped_column(String(16), nullable=False)  # paper | jupiter | cex
    mint: Mapped[str | None] = mapped_column(String(64))
    initial_stop: Mapped[float] = mapped_column(Float, nullable=False)
    risk_usd: Mapped[float] = mapped_column(Float, nullable=False)
    highest_price: Mapped[float] = mapped_column(Float, nullable=False)
    last_price: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(String(64))
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    entry_order_id: Mapped[str | None] = mapped_column(String(128))
    exit_order_id: Mapped[str | None] = mapped_column(String(128))
    stop_order_id: Mapped[str | None] = mapped_column(String(128))


class AgentDecision(Base):
    __tablename__ = "agent_decisions"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class SystemLog(Base):
    __tablename__ = "system_logs"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    module: Mapped[str | None] = mapped_column(String(128))


class AgentHeartbeat(Base):
    __tablename__ = "agent_heartbeats"
    agent: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_latency_ms: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)


class ControlState(Base):
    """Single-row table (id=1): the dashboard writes it, the engine reads it every cycle."""

    __tablename__ = "control_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    halted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    close_all_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    risk_pct: Mapped[float] = mapped_column(Float, nullable=False)
    kelly_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    equity_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    var_usd: Mapped[float] = mapped_column(Float, nullable=False)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)


HYPERTABLES: tuple[str, ...] = (MarketTick.__tablename__, WhaleTransaction.__tablename__)
