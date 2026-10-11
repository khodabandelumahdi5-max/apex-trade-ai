"""python -m tgbot   — needs TELEGRAM_BOT_TOKEN in .env (from @BotFather). Run ONE copy only."""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger


def main() -> int:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env (create a bot with @BotFather). Never share the token.")
        return 2

    from config import get_settings
    from database.connection import init_db
    from exchange_connector import MexcSpotConnector
    from tgbot.app import Bot

    settings = get_settings()
    asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().run_until_complete(init_db(settings.database_url))
    bot = Bot(token, MexcSpotConnector(settings))
    logger.info("AlphaDuel bot polling (Ctrl+C to stop)")
    bot.app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
