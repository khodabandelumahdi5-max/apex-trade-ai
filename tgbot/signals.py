"""Owner-only trading signals in the Telegram bot (ADMIN_TELEGRAM_IDS receive them).

Jobs: scan every SIGNAL_SCAN_MINUTES for new BUY signals and trend exits; watch open signals every
SIGNAL_WATCH_SECONDS for danger / stop / target / stop-move alerts. Commands: /signals /plan /scan
/entered ID PRICE /exit ID [PRICE]. Buttons under each signal: I entered / Skip / I sold.
"""
from __future__ import annotations

import os
import time

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from agents.technical_agent import TechnicalAgent
from config import WatchToken
from database.connection import get_session
from signalbot import service as sigsvc
from signalbot.core import Alert, SignalConfig, e, px, signal_text
from tgbot import service as svc

OUTAGE_ALERT_SECONDS = 180


def owner_ids() -> list[int]:
    return [int(x) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").replace(" ", "").split(",") if x.isdigit()]


class SignalDesk:
    def __init__(self, app, mexc) -> None:
        self.app, self.mexc = app, mexc
        self.settings = mexc.settings
        self.cfg = SignalConfig.from_env()
        self.tech = TechnicalAgent(self.settings, None, None, None, mexc)
        self._fail_since: float | None = None
        self._outage_sent = False
        self._last_scan_errors: dict[str, str] = {}
        for name, fn in {"signals": self.cmd_signals, "plan": self.cmd_plan, "scan": self.cmd_scan,
                         "entered": self.cmd_entered, "exit": self.cmd_exit}.items():
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(self.on_button, pattern=r"^sig:"))
        if os.getenv("SIGNALS_ENABLED", "1") != "0" and owner_ids():
            app.job_queue.run_repeating(self.scan_job, interval=self.cfg.scan_minutes * 60, first=15)
            app.job_queue.run_repeating(self.watch_job, interval=self.cfg.watch_seconds, first=25)
            logger.info("Signals on for {} → owners {}", ",".join(self.cfg.symbols), owner_ids())
        else:
            logger.warning("Signals off: set ADMIN_TELEGRAM_IDS (your numeric Telegram id) in .env")

    # ------------------------------------------------------------------ data
    async def tech_fn(self, symbol: str):
        if not await self.mexc.has_symbol(symbol):
            raise ValueError(f"{symbol}/USDT is not on MEXC")
        market = await self.mexc.fetch_ticker(symbol, depth=20)
        tech = await self.tech.process(token=WatchToken(symbol=symbol, mint=symbol), market=market)
        return tech, market

    async def quote_fn(self, symbol: str) -> tuple[float, float]:
        m = await self.mexc.fetch_ticker(symbol, depth=5)
        return (m.bid or m.price), (m.ask or m.price)

    # ------------------------------------------------------------------ sending
    async def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> int | None:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in buttons]]) if buttons else None
        mid = None
        for chat in owner_ids():
            try:
                msg = await self.app.bot.send_message(chat, text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                                      disable_web_page_preview=True)
                mid = msg.message_id
            except TelegramError as exc:
                logger.warning("signal message to {} failed: {} (did you press Start in the bot?)", chat, exc)
        return mid

    async def send_alerts(self, alerts: list[Alert]) -> None:
        for a in alerts:
            await self.send(a.text, a.buttons)

    # ------------------------------------------------------------------ jobs
    async def scan_job(self, context: ContextTypes.DEFAULT_TYPE | None = None) -> int:
        async with svc.LOCK, get_session() as s:
            created, alerts, errors = await sigsvc.scan(s, self.tech_fn, self.settings, self.cfg)
            texts = [(sig.id, signal_text(sig, self.cfg)) for sig in created]
        for err in errors:
            sym = err.split(":")[0]
            if self._last_scan_errors.get(sym) != err:       # log each distinct error once
                logger.warning("signal scan {}", err)
            self._last_scan_errors[sym] = err
        for sid, text in texts:
            await self.send(text, [("✅ وارد شدم", f"sig:enter:{sid}"), ("⏭ رد کردم", f"sig:skip:{sid}")])
        await self.send_alerts(alerts)
        return len(texts)

    async def watch_job(self, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        try:
            async with svc.LOCK, get_session() as s:
                alerts = await sigsvc.watch(s, self.quote_fn, self.settings, self.cfg)
                entered = any(sig.status == "ENTERED" for sig in await sigsvc.active_signals(s))
        except Exception as exc:
            now = time.monotonic()
            self._fail_since = self._fail_since or now
            logger.warning("signal watch failed: {}", exc)
            if now - self._fail_since > OUTAGE_ALERT_SECONDS and not self._outage_sent:
                self._outage_sent = True
                await self.send("📡 <b>قیمت‌ها چند دقیقه است در دسترس نیستند</b> (اینترنت/VPN یا MEXC). تا برگردد "
                                "هشداری نمی‌رسد. <b>به حد ضرری که در صرافی ثبت کرده‌ای تکیه کن.</b>")
            return
        if self._outage_sent:
            await self.send("✅ اتصال به قیمت‌ها برگشت؛ مراقبت ادامه دارد.")
        self._fail_since, self._outage_sent = None, False
        await self.send_alerts(alerts)

    # ------------------------------------------------------------------ owner commands
    @staticmethod
    def _owner(update: Update) -> bool:
        return update.effective_user is not None and update.effective_user.id in owner_ids()

    async def _reply(self, update: Update, text: str) -> None:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._owner(update):
            return
        async with get_session() as s:
            sigs = await sigsvc.active_signals(s)
        prices = {}
        for sym in {x.symbol for x in sigs}:
            try:
                prices[sym] = (await self.quote_fn(sym))[0]
            except Exception:
                pass
        await self._reply(update, sigsvc.list_text(sigs, prices))

    async def cmd_plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._owner(update):
            return
        rows = []
        for sym in self.cfg.symbols:
            try:
                tech, market = await self.tech_fn(sym)
                rows.append((sym, tech, market, None))
            except Exception as exc:
                rows.append((sym, None, None, str(exc)[:80]))
        await self._reply(update, sigsvc.plan_text(rows))

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._owner(update):
            return
        n = await self.scan_job()
        if not n:
            await self._reply(update, "بررسی شد: الان سیگنال خرید جدیدی نیست. وضعیت هر ارز: /plan")

    async def cmd_entered(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._owner(update):
            return
        args = context.args or []
        if len(args) != 2:
            return await self._reply(update, "استفاده: <code>/entered شماره قیمت</code> مثلاً <code>/entered 3 142.5</code>")
        try:
            price = float(args[1].replace(",", ""))
            async with svc.LOCK, get_session() as s:
                text = sigsvc.enter(await sigsvc.get_signal(s, args[0]), price, self.settings, self.cfg)
        except (ValueError, sigsvc.SignalError) as exc:
            text = f"⚠️ {e(str(exc))}"
        await self._reply(update, text)

    async def cmd_exit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._owner(update):
            return
        args = context.args or []
        if not 1 <= len(args) <= 2:
            return await self._reply(update, "استفاده: <code>/exit شماره [قیمت فروش]</code>")
        try:
            async with svc.LOCK, get_session() as s:
                sig = await sigsvc.get_signal(s, args[0])
                price = float(args[1].replace(",", "")) if len(args) == 2 else (await self.quote_fn(sig.symbol))[0]
                text = sigsvc.close(sig, price, "manual")
        except (ValueError, sigsvc.SignalError) as exc:
            text = f"⚠️ {e(str(exc))}"
        await self._reply(update, text)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        if not self._owner(update):
            return
        _, action, raw = (q.data or "").split(":", 2) if (q.data or "").count(":") == 2 else ("", "", "")
        try:
            async with svc.LOCK, get_session() as s:
                sig = await sigsvc.get_signal(s, raw)
                if action == "enter":
                    bid, ask = await self.quote_fn(sig.symbol)
                    text = sigsvc.enter(sig, ask, self.settings, self.cfg) + \
                        f"\n(قیمت ورود با قیمت فعلی {px(ask)} ثبت شد؛ اگر قیمت خریدت فرق دارد: " \
                        f"<code>/entered {sig.id} قیمت</code>)"
                elif action == "skip":
                    text = sigsvc.close(sig, None, "skipped")
                elif action == "exit":
                    bid, _ = await self.quote_fn(sig.symbol)
                    text = sigsvc.close(sig, bid, "manual")
                else:
                    return
        except sigsvc.SignalError as exc:
            text = f"⚠️ {e(str(exc))}"
        except Exception as exc:
            logger.warning("signal button failed: {}", exc)
            text = "⚠️ قیمت در دسترس نیست؛ چند ثانیه بعد دوباره بزن."
        await self._reply(update, text)
