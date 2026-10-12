"""Configure the Telegram bot itself at start-up (instead of clicking through @BotFather):
commands menu, description, short description and the Mini App menu button.

Safety: when BOT_USERNAME is set, the bot refuses to start with a token that belongs to another bot, so a
copied .env can never make the wrong bot take payments.
"""
from __future__ import annotations

import os

from loguru import logger
from telegram import Bot, BotCommand, BotCommandScopeChat, MenuButtonCommands, MenuButtonWebApp, WebAppInfo
from telegram.error import TelegramError

COMMANDS = [
    ("start", "Main menu"), ("subscribe", "Subscribe"), ("status", "My subscription"),
    ("challenges", "Live challenges"), ("join", "Join a challenge: /join ID"),
    ("buy", "Buy: /buy ID SYMBOL USD STOP"), ("sell", "Sell: /sell ID SYMBOL [QTY]"),
    ("stop", "Raise a stop: /stop ID SYMBOL PRICE"), ("me", "My stats: /me ID"),
    ("board", "Leaderboard: /board ID"), ("help", "How it works"), ("terms", "Terms and privacy"),
    ("paysupport", "Payment support"),
]
OWNER_COMMANDS = [
    ("signals", "Active trading signals"), ("plan", "Market state now"), ("scan", "Check for signals now"),
    ("entered", "I bought: /entered ID PRICE"), ("exit", "I sold: /exit ID [PRICE]"),
]
DESCRIPTION = (
    "⚡ AlphaDuel — the 21-day trading discipline challenge.\n\n"
    "Trade a virtual balance at live market prices, follow strict risk rules (stop-loss on every trade, daily loss "
    "limit, max drawdown) and climb the leaderboard by discipline and return.\n\n"
    "No exchange account, no API keys, no real trading. Not financial advice.")
SHORT_DESCRIPTION = "21-day paper-trading discipline challenge with a live leaderboard. Not financial advice."


class WrongBotError(RuntimeError):
    pass


async def configure(bot: Bot) -> str:
    me = await bot.get_me()
    expected = os.getenv("BOT_USERNAME", "").strip().lstrip("@").lower()
    if expected and me.username.lower() != expected:
        raise WrongBotError(f"TELEGRAM_BOT_TOKEN belongs to @{me.username}, but BOT_USERNAME is @{expected}")
    try:
        await bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        owners = [int(x) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").replace(" ", "").split(",") if x.isdigit()]
        for chat in owners:                       # owner-only commands show up only in the owner's chat
            await bot.set_my_commands([BotCommand(c, d) for c, d in OWNER_COMMANDS + COMMANDS],
                                      scope=BotCommandScopeChat(chat))
        if (await bot.get_my_description()).description != DESCRIPTION:     # avoid rate-limited rewrites
            await bot.set_my_description(DESCRIPTION)
        if (await bot.get_my_short_description()).short_description != SHORT_DESCRIPTION:
            await bot.set_my_short_description(SHORT_DESCRIPTION)
        url = os.getenv("MINIAPP_URL", "").strip()
        if url.startswith("https://"):
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Open app", web_app=WebAppInfo(url=url)))
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramError as exc:                    # cosmetic settings must never stop the bot
        logger.warning("bot profile setup incomplete: {}", exc)
    logger.info("Running as @{} (id {})", me.username, me.id)
    owners = [int(x) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").replace(" ", "").split(",") if x.isdigit()]
    for chat in owners:                           # after a reboot / reconnect the owner knows it is watching again
        try:
            await bot.send_message(chat, "✅ ربات روشن شد و بازار را رصد می‌کند. وضعیت: /plan · سیگنال‌های باز: /signals")
        except TelegramError as exc:
            logger.warning("start-up message to {} failed: {}", chat, exc)
    return me.username
