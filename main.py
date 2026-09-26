"""Apex Trade AI — async 24/7 entry point."""
from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger
from pydantic import ValidationError

from config import get_settings
from core.coordinator import SwarmCoordinator
from database.connection import DatabaseError, dispose_engine, init_db
from database.repository import db_log_sink


def configure_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", enqueue=False,
               format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
                      "<cyan>{name}</cyan> - <level>{message}</level>")
    logger.add("logs/apex_{time:YYYY-MM-DD}.log", level="DEBUG", rotation="00:00", retention="14 days",
               compression="gz", enqueue=True)


async def run() -> int:
    configure_logging()
    try:
        settings = get_settings()
    except (ValidationError, ValueError) as exc:
        logger.critical("invalid configuration: {}", exc)
        return 2
    if settings.is_live:
        logger.warning("⚠️  LIVE TRADING ENABLED — real funds are at risk")
    else:
        logger.info("Paper-trading mode: fills are priced from live Jupiter quotes, no funds move")

    try:
        await init_db(settings.database_url)
    except DatabaseError as exc:
        logger.critical("{}", exc)
        return 3
    logger.add(db_log_sink, level="WARNING")

    coordinator = SwarmCoordinator(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, coordinator.request_stop)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: coordinator.request_stop())

    exit_code = 0
    try:
        await coordinator.start()
        await coordinator.run_forever()
    except Exception as exc:
        logger.exception("fatal engine error: {!r}", exc)
        exit_code = 1
    finally:
        logger.info("shutting down: CEX stops stay on the exchange; paper/DEX stops are only enforced while the engine runs")
        await coordinator.shutdown()
        await logger.complete()
        await dispose_engine()
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
