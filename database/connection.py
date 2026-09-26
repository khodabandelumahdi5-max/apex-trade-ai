"""Async engine / session factory and startup initialisation."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from database.models import HYPERTABLES, Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class DatabaseError(RuntimeError):
    pass


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def get_engine(database_url: str) -> AsyncEngine:
    """Create (once) and return the process-wide async engine."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
    if _is_postgres(database_url):
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800, pool_timeout=30)
    else:
        kwargs["connect_args"] = {"timeout": 30}
    try:
        _engine = create_async_engine(database_url, **kwargs)
    except (SQLAlchemyError, ValueError) as exc:
        raise DatabaseError(f"Cannot create engine for {database_url.split('@')[-1]}: {exc}") from exc
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise DatabaseError("Database not initialised; call get_engine()/init_db() first")
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commits on success, rolls back on any error."""
    session = session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _setup_timescale(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        available = (await conn.execute(text(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"))).scalar()
        if not available:
            logger.info("TimescaleDB extension not available; using plain PostgreSQL tables")
            return
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        for table in HYPERTABLES:
            is_hyper = (await conn.execute(text(
                "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = :t"),
                {"t": table})).scalar()
            if is_hyper:
                continue
            # Hypertable unique constraints must include the time column.
            await conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{table}_pkey"'))
            await conn.execute(text(f'ALTER TABLE "{table}" ADD PRIMARY KEY (id, "timestamp")'))
            await conn.execute(text(
                f"SELECT create_hypertable('{table}', 'timestamp', migrate_data => TRUE, if_not_exists => TRUE)"))
            logger.info("Converted {} to a TimescaleDB hypertable", table)


async def init_db(database_url: str, retries: int = 5, base_delay: float = 2.0) -> AsyncEngine:
    """Create tables (and hypertables on TimescaleDB), retrying while the DB comes up."""
    engine = get_engine(database_url)
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                if not _is_postgres(database_url):
                    await conn.execute(text("PRAGMA journal_mode=WAL"))
                await conn.run_sync(Base.metadata.create_all)
            if _is_postgres(database_url):
                await _setup_timescale(engine)
            logger.info("Database ready ({})", engine.url.render_as_string(hide_password=True))
            return engine
        except (OperationalError, DBAPIError, OSError) as exc:
            if attempt == retries:
                raise DatabaseError(f"Database unreachable after {retries} attempts: {exc}") from exc
            delay = base_delay * 2 ** (attempt - 1)
            logger.warning("DB init attempt {}/{} failed ({}); retrying in {:.0f}s", attempt, retries, exc, delay)
            await asyncio.sleep(delay)
    raise DatabaseError("unreachable")


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine, _session_factory = None, None
