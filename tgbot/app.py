"""Telegram front-end (python-telegram-bot 21, long polling: no public URL or webhook needed).

The same process serves the Mini App (webapp/server.py) on MINIAPP_HOST:MINIAPP_PORT (default 127.0.0.1:8080,
MINIAPP_PORT=0 disables it), so bot commands, app requests and jobs share one write lock and one price cache.

Background jobs (every 60 s): match USDT payments and notify payers; tick every live challenge (stops, rules,
ranking) and push stop / near-stop / cooldown / disqualification messages to the trader.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from billing.service import apply_transfers, expire_invoices
from billing.tron import TronGridClient, is_tron_address
from competition import engine
from competition.engine import ChallengeError, Quote
from database.connection import get_session
from database.models import Challenge, Invoice, InvoiceStatus, User
from tgbot import service as svc

HELP = (
    "<b>AlphaDuel — 21-day trading discipline challenge</b>\n"
    "Paper trading with a virtual balance at live MEXC prices. No exchange keys, no deposits to an exchange.\n\n"
    "/subscribe – USDT (TRC20) invoice\n/status – your subscription\n/challenges – live challenges\n"
    "/join ID\n/buy ID SYMBOL USD STOP  e.g. <code>/buy 1 SOL 500 140</code>\n"
    "/sell ID SYMBOL [QTY]\n/stop ID SYMBOL NEW_STOP  (raise only)\n/me ID – your stats\n/board ID – top 10\n\n"
    "Not financial advice. Results in a paper challenge do not predict real trading results.")


class Bot:
    def __init__(self, token: str, mexc) -> None:
        self.mexc = mexc
        self.token = token
        self._web_runner = None
        self.app = (Application.builder().token(token).concurrent_updates(True)
                    .post_init(self._start_web).post_shutdown(self._stop_web).build())
        self._last_cmd: dict[int, float] = {}
        self._quote_cache: dict[str, tuple[float, Quote]] = {}
        a = self.app
        for name, fn in {"start": self.start, "menu": self.start, "help": self.help, "subscribe": self.subscribe,
                         "status": self.status, "challenges": self.challenges, "join": self.join,
                         "me": self.me, "board": self.board}.items():
            a.add_handler(CommandHandler(name, fn))
        for name in ("buy", "sell", "stop"):
            a.add_handler(CommandHandler(name, self.trade))
        a.add_handler(CallbackQueryHandler(self.button))
        a.job_queue.run_repeating(self.payments_job, interval=60, first=10)
        a.job_queue.run_repeating(self.challenges_job, interval=60, first=20)

    # ------------------------------------------------------------------ Mini App server (same process)
    async def _start_web(self, _: Application) -> None:
        if os.getenv("MINIAPP_PORT", "8080").strip() in {"", "0"}:
            return
        from webapp.server import build_app, start_site
        self._web_runner = await start_site(build_app(self.token, self.quote))
        logger.info("Mini App server on http://{}:{}", os.getenv("MINIAPP_HOST", "127.0.0.1"),
                    os.getenv("MINIAPP_PORT", "8080"))

    async def _stop_web(self, _: Application) -> None:
        if self._web_runner is not None:
            await self._web_runner.cleanup()

    # ------------------------------------------------------------------ helpers
    def _throttled(self, uid: int) -> bool:
        now = time.monotonic()
        if now - self._last_cmd.get(uid, 0) < 1.0:              # 1 command / second / user
            return True
        self._last_cmd[uid] = now
        return False

    async def quote(self, symbol: str) -> Quote:
        hit = self._quote_cache.get(symbol)
        if hit and time.monotonic() - hit[0] < 3:                # protects the MEXC rate limit
            return hit[1]
        m = await self.mexc.fetch_ticker(symbol, depth=5)
        q = Quote(bid=m.bid or m.price, ask=m.ask or m.price)
        self._quote_cache[symbol] = (time.monotonic(), q)
        return q

    def _menu(self) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton("💎 Subscribe", callback_data="sub"),
                 InlineKeyboardButton("📊 My status", callback_data="status")],
                [InlineKeyboardButton("🏆 Challenges", callback_data="chal")]]
        url = os.getenv("MINIAPP_URL", "").strip()
        if url.startswith("https://"):
            rows.append([InlineKeyboardButton("📱 Open leaderboard app", web_app=WebAppInfo(url=url))])
        return InlineKeyboardMarkup(rows)

    async def _reply(self, update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        msg = update.effective_message
        if msg is not None:
            await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                 disable_web_page_preview=True)

    async def _user(self, s, update: Update) -> User:
        u = update.effective_user
        return await svc.get_or_create_user(s, u.id, u.first_name)

    # ------------------------------------------------------------------ commands
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with svc.LOCK, get_session() as s:
            user = await self._user(s, update)
            text = f"⚡ Welcome to <b>AlphaDuel</b>, {svc.e(user.display_name or 'trader')}!\n\n" \
                   f"{svc.subscription_text(user)}"
        await self._reply(update, text, self._menu())

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, HELP)

    async def subscribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with svc.LOCK, get_session() as s:
            text = await svc.invoice_text(s, await self._user(s, update))
        await self._reply(update, text)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with svc.LOCK, get_session() as s:
            text = svc.subscription_text(await self._user(s, update))
        await self._reply(update, text)

    async def challenges(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with get_session() as s:
            chs = await svc.live_challenges(s)
        if not chs:
            await self._reply(update, "No live challenges right now.")
        for ch in chs[:5]:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Join", callback_data=f"join:{ch.id}"),
                                        InlineKeyboardButton("🏁 Leaderboard", callback_data=f"board:{ch.id}"),
                                        InlineKeyboardButton("📊 Me", callback_data=f"me:{ch.id}")]])
            await self._reply(update, svc.challenge_text(ch), kb)

    async def _join(self, update: Update, raw_id: str) -> str:
        async with svc.LOCK, get_session() as s:
            user = await self._user(s, update)
            try:
                ch = await svc.load_challenge(s, raw_id)
                p = await engine.join(s, ch, user)
                return (f"✅ You are in <b>#{ch.id} {svc.e(ch.title)}</b> with "
                        f"${p.equity:,.0f} virtual USDT.\nEvery buy needs a stop: "
                        f"<code>/buy {ch.id} {ch.symbol_list[0]} 500 STOP</code>")
            except ChallengeError as exc:
                return f"⚠️ {svc.e(str(exc))}"

    async def join(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            return await self._reply(update, "Usage: <code>/join ID</code>")
        await self._reply(update, await self._join(update, context.args[0]))

    async def _me(self, update: Update, raw_id: str) -> str:
        async with svc.LOCK, get_session() as s:
            user = await self._user(s, update)
            try:
                ch = await svc.load_challenge(s, raw_id)
                return svc.participant_text(ch, await engine.get_participant(s, ch, user))
            except ChallengeError as exc:
                return f"⚠️ {svc.e(str(exc))}"

    async def me(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            return await self._reply(update, "Usage: <code>/me ID</code>")
        await self._reply(update, await self._me(update, context.args[0]))

    async def _board(self, raw_id: str) -> str:
        async with get_session() as s:
            try:
                ch = await svc.load_challenge(s, raw_id)
            except ChallengeError as exc:
                return f"⚠️ {svc.e(str(exc))}"
            rows = engine.rank(await engine.participants(s, ch))
            ids = [p.user_id for p in rows]
            users = (await s.execute(select(User).where(User.id.in_(ids)))).scalars() if ids else []
            return svc.board_text(ch, rows, {u.id: engine.public_name(u) for u in users})

    async def board(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            return await self._reply(update, "Usage: <code>/board ID</code>")
        await self._reply(update, await self._board(context.args[0]))

    async def trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._throttled(update.effective_user.id):
            return
        cmd = update.effective_message.text.split()[0].lstrip("/").split("@")[0].lower()
        try:
            async with svc.LOCK, get_session() as s:
                text = await svc.trade_command(s, await self._user(s, update), cmd, list(context.args or []),
                                               self.quote)
        except Exception as exc:                                  # MEXC down etc.: never crash the handler
            logger.warning("trade command failed: {}", exc)
            text = "⚠️ Prices are unavailable right now, try again in a minute."
        await self._reply(update, text)

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        if self._throttled(update.effective_user.id):
            return
        data = q.data or ""
        kind, _, arg = data.partition(":")
        if kind == "sub":
            await self.subscribe(update, context)
        elif kind == "status":
            await self.status(update, context)
        elif kind == "chal":
            await self.challenges(update, context)
        elif kind == "join" and arg.isdigit():
            await self._reply(update, await self._join(update, arg))
        elif kind == "board" and arg.isdigit():
            await self._reply(update, await self._board(arg))
        elif kind == "me" and arg.isdigit():
            await self._reply(update, await self._me(update, arg))

    # ------------------------------------------------------------------ jobs
    async def _send(self, user_id: int, text: str) -> None:
        async with get_session() as s:
            tg = (await s.execute(select(User.telegram_id).where(User.id == user_id))).scalar()
        if not tg:
            return
        try:
            await self.app.bot.send_message(tg, text, parse_mode=ParseMode.HTML)
        except Forbidden:
            logger.info("user {} blocked the bot", user_id)
        except TelegramError as exc:
            logger.warning("send to user {} failed: {}", user_id, exc)

    async def payments_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        address = os.getenv("TRON_RECEIVE_ADDRESS", "").strip()
        if not is_tron_address(address):
            return
        try:
            async with svc.LOCK, get_session() as s:
                await expire_invoices(s)
                since = (await s.execute(select(func.min(Invoice.created_at))
                                         .where(Invoice.status == InvoiceStatus.PENDING))).scalar()
            if since is None:
                return
            since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            transfers = await TronGridClient(address, os.getenv("TRONGRID_API_KEY") or None).incoming_usdt(since)
            async with svc.LOCK, get_session() as s:
                paid = await apply_transfers(s, transfers, address)
                notes = [(inv.user_id, inv.amount_usdt) for inv in paid]
            for user_id, amount in notes:
                async with get_session() as s:
                    user = await s.get(User, user_id)
                    text = f"✅ Payment of {amount} USDT received.\n{svc.subscription_text(user)}"
                await self._send(user_id, text)
        except Exception as exc:
            logger.warning("payment check failed: {}", exc)

    async def challenges_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(timezone.utc)
        async with get_session() as s:
            ids = [c.id for c in (await s.execute(select(Challenge).where(Challenge.finalized.is_(False))))
                   .scalars() if svc._aware(c.start_time) <= now]
        for cid in ids:
            try:
                async with get_session() as s:
                    symbols = (await s.get(Challenge, cid)).symbol_list
                quotes = {sym: await self.quote(sym) for sym in symbols}
                async with svc.LOCK, get_session() as s:
                    events = await engine.tick(s, await s.get(Challenge, cid), quotes)
                for ev in events:
                    await self._send(ev.user_id, svc.event_text(ev))
            except Exception as exc:                              # one bad challenge must not stop the others
                logger.warning("tick of challenge {} failed: {}", cid, exc)
