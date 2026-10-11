"""aiohttp backend of the AlphaDuel Mini App.

Runs INSIDE the Telegram bot process (`python -m tgbot` starts it), so the app, the bot commands and the
background jobs share one write lock and one price cache: no lost updates between processes, one MEXC client.
`python -m webapp` starts it alone in read-only mode (no trading endpoints), e.g. for testing the page.

Every /api call except the public ones needs the header X-Telegram-Init-Data (signed by Telegram, see auth.py).
Auth is a header, not a cookie, so cross-site requests (CSRF) can't act for a user.

GET  /api/challenges                    public  challenge list with rules
GET  /api/challenges/{id}/leaderboard   public  ranked traders (public names only)
GET  /api/quotes?challenge={id}         public  bid/ask of the challenge symbols
GET  /api/session                       user    account + subscription (registers on first open)
GET  /api/me?challenge={id}             user    stats, positions, equity curve
POST /api/invoice                       user    open USDT (TRC20) invoice
POST /api/join      {challenge}
POST /api/max_buy   {challenge, symbol, stop}                       largest buy within the risk rule
POST /api/order     {challenge, side: BUY|SELL, symbol, usd, stop | qty}
POST /api/stop      {challenge, symbol, stop}
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from sqlalchemy import select

from competition import engine
from competition.engine import ChallengeError, Quote, RuleViolation
from database.connection import get_session
from database.models import Challenge, ChallengeEquityPoint, User
from tgbot import service as svc
from webapp.auth import InitDataError, verify_init_data

STATIC = Path(__file__).parent / "static"
BOT_TOKEN = web.AppKey("bot_token", str)
QUOTES = web.AppKey("quotes", object)          # async (symbol) -> Quote, or None in read-only mode
THROTTLE = web.AppKey("throttle", dict)
QuoteFn = Callable[[str], Awaitable[Quote]]


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _bad(msg: str, *, rule: bool = False, status: int = 400) -> web.HTTPException:
    cls = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 404: web.HTTPNotFound, 429: web.HTTPTooManyRequests,
           503: web.HTTPServiceUnavailable}[status]
    return cls(text=web.json_response({"error": msg, "rule": rule}).text, content_type="application/json")


def _int(raw: object, what: str = "challenge") -> int:
    if isinstance(raw, bool) or not (isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit() and len(raw) < 13)):
        raise _bad(f"bad {what} id")
    return int(raw)


def _pos(raw: object, what: str, required: bool = True) -> float | None:
    if raw is None and not required:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise _bad(f"{what} must be a number")
    try:
        v = float(raw)
    except ValueError:
        raise _bad(f"{what} must be a number") from None
    if not math.isfinite(v) or v <= 0:
        raise _bad(f"{what} must be a positive number")
    return v


@web.middleware
async def security_headers(request: web.Request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' "
                            "'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors "
                            "https://web.telegram.org https://*.telegram.org")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _tg_user(request: web.Request) -> dict:
    try:
        return verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), request.app[BOT_TOKEN])
    except InitDataError as exc:
        raise _bad(str(exc), status=401) from None


def _throttle(request: web.Request, tg_id: int) -> None:
    last = request.app[THROTTLE]
    now = time.monotonic()
    if now - last.get(tg_id, 0.0) < 0.5:                        # 2 writes / second / user
        raise _bad("too many requests, slow down", status=429)
    last[tg_id] = now


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise _bad("JSON body required") from None
    if not isinstance(data, dict):
        raise _bad("JSON object required")
    return data


def _quotes(request: web.Request) -> QuoteFn:
    fn = request.app[QUOTES]
    if fn is None:
        raise _bad("trading is only available while the bot is running", status=503)
    return fn  # type: ignore[return-value]


async def _quote(fn: QuoteFn, symbol: str) -> Quote:
    try:
        return await asyncio.wait_for(fn(symbol), timeout=15)
    except ChallengeError:
        raise
    except Exception:
        raise _bad("prices are unavailable right now, try again in a minute", status=503) from None


async def _challenge(s, raw: object) -> Challenge:
    ch = await s.get(Challenge, _int(raw))
    if ch is None:
        raise _bad("no such challenge", status=404)
    return ch


# ---------------------------------------------------------------------------------------------- public
async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def app_js(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "app.js")


async def challenges(_: web.Request) -> web.Response:
    now = datetime.now(timezone.utc)
    async with get_session() as s:
        rows = (await s.execute(select(Challenge).order_by(Challenge.start_time.desc()).limit(20))).scalars().all()
        out = [{"id": c.id, "title": c.title, "symbols": c.symbol_list, "prize": c.prize_text,
                "start": _iso(c.start_time), "end": _iso(c.end_time), "final": c.finalized,
                "running": not c.finalized and engine._aware(c.start_time) <= now < engine._aware(c.end_time),
                "starting_balance": c.starting_balance, "fee_pct": c.fee_bps / 100,
                "rules": {"risk_per_trade_pct": c.max_risk_per_trade_pct, "daily_loss_pct": c.daily_loss_limit_pct,
                          "max_drawdown_pct": c.max_drawdown_pct, "losses_before_pause": c.max_consecutive_losses,
                          "pause_hours": c.cooldown_hours, "entries_per_day": c.max_entries_per_day}}
               for c in rows]
    return web.json_response(out)


async def leaderboard(request: web.Request) -> web.Response:
    async with get_session() as s:
        ch = await _challenge(s, request.match_info.get("cid"))
        rows = engine.rank(await engine.participants(s, ch))
        ids = [p.user_id for p in rows] or [-1]
        names = {u.id: engine.public_name(u) for u in (await s.execute(select(User).where(User.id.in_(ids)))).scalars()}
        out = [{"rank": p.rank, "name": names.get(p.user_id, "?"), "roi": round(p.roi_pct, 4),
                "discipline": engine.discipline_score(p), "max_dd": round(p.max_drawdown_pct, 4),
                "trades": p.trades_count, "status": p.status} for p in rows[:100]]
    return web.json_response({"challenge": ch.title, "final": ch.finalized, "rows": out})


async def quotes(request: web.Request) -> web.Response:
    fn = _quotes(request)
    async with get_session() as s:
        ch = await _challenge(s, request.query.get("challenge"))
        symbols = ch.symbol_list
    out = {}
    for sym in symbols:
        q = await _quote(fn, sym)
        out[sym] = {"bid": q.bid, "ask": q.ask}
    return web.json_response(out)


# ---------------------------------------------------------------------------------------------- user
async def session(request: web.Request) -> web.Response:
    tg = _tg_user(request)
    async with svc.LOCK, get_session() as s:
        user = await svc.get_or_create_user(s, tg["id"], tg.get("first_name"))
        return web.json_response({"name": engine.public_name(user), "subscribed": user.subscription_active,
                                  "expires": _iso(user.subscription_expires_at),
                                  "payments": bool(os.getenv("TRON_RECEIVE_ADDRESS", "").strip())})


async def me(request: web.Request) -> web.Response:
    tg = _tg_user(request)
    async with get_session() as s:
        user = (await s.execute(select(User).where(User.telegram_id == tg["id"]))).scalar()
        ch = await _challenge(s, request.query.get("challenge"))
        if user is None:
            return web.json_response({"joined": False, "subscribed": False})
        try:
            p = await engine.get_participant(s, ch, user)
        except ChallengeError:
            return web.json_response({"joined": False, "subscribed": user.subscription_active})
        points = (await s.execute(select(ChallengeEquityPoint).where(ChallengeEquityPoint.participant_id == p.id)
                                  .order_by(ChallengeEquityPoint.ts))).scalars().all()
        f = ch.fee_bps / 10_000
        return web.json_response({
            "joined": True, "subscribed": user.subscription_active, "status": p.status, "reason": p.status_reason,
            "rank": p.rank, "roi": p.roi_pct, "equity": p.equity, "cash": p.cash, "max_dd": p.max_drawdown_pct,
            "discipline": engine.discipline_score(p), "rule_breaks": p.rule_breaks, "wins": p.wins,
            "losses": p.losses, "entries_today": p.entries_today, "cooldown_until": _iso(p.cooldown_until),
            "risk_budget": p.equity * ch.max_risk_per_trade_pct / 100,
            "positions": [{"symbol": k, "qty": v["qty"], "avg": v["cost"] / v["qty"], "stop": v["stop"],
                           "risk": engine.stop_loss_risk(v["qty"], v["cost"], v["stop"], f)}
                          for k, v in (p.holdings or {}).items()],
            "curve": [[_iso(pt.ts), round(pt.equity, 2)] for pt in points[-2000:]]})


async def invoice(request: web.Request) -> web.Response:
    tg = _tg_user(request)
    _throttle(request, tg["id"])
    async with svc.LOCK, get_session() as s:
        user = await svc.get_or_create_user(s, tg["id"], tg.get("first_name"))
        inv = await svc.open_invoice(s, user)
        if inv is None:
            raise _bad("payments are not configured yet", status=503)
        return web.json_response({"amount": inv.amount_usdt, "address": inv.address, "network": "TRON (TRC20)",
                                  "token": "USDT", "days": inv.days, "expires": _iso(inv.expires_at)})


async def join(request: web.Request) -> web.Response:
    tg = _tg_user(request)
    _throttle(request, tg["id"])
    body = await _body(request)
    async with svc.LOCK, get_session() as s:
        user = await svc.get_or_create_user(s, tg["id"], tg.get("first_name"))
        ch = await _challenge(s, body.get("challenge"))
        try:
            p = await engine.join(s, ch, user)
        except ChallengeError as exc:
            raise _bad(str(exc)) from None
        return web.json_response({"ok": True, "equity": p.equity})


async def max_buy(request: web.Request) -> web.Response:
    tg = _tg_user(request)
    body = await _body(request)
    fn = _quotes(request)
    async with get_session() as s:
        user = (await s.execute(select(User).where(User.telegram_id == tg["id"]))).scalar()
        ch = await _challenge(s, body.get("challenge"))
        if user is None:
            raise _bad("join the challenge first")
        try:
            p = await engine.get_participant(s, ch, user)
        except ChallengeError as exc:
            raise _bad(str(exc)) from None
        symbol = str(body.get("symbol", "")).upper()
        if symbol not in ch.symbol_list:
            raise _bad(f"{symbol or '?'} is not traded in this challenge")
        stop = _pos(body.get("stop"), "stop")
        q = await _quote(fn, symbol)
        return web.json_response({"max_usd": engine.max_buy_usd(ch, p, symbol, q, stop), "ask": q.ask, "bid": q.bid})


async def _trade(request: web.Request, action: str) -> web.Response:
    tg = _tg_user(request)
    _throttle(request, tg["id"])
    body = await _body(request)
    fn = _quotes(request)
    async with svc.LOCK, get_session() as s:
        user = await svc.get_or_create_user(s, tg["id"], tg.get("first_name"))
        ch = await _challenge(s, body.get("challenge"))
        symbol = str(body.get("symbol", "")).upper()
        if symbol not in ch.symbol_list:
            raise _bad(f"{symbol or '?'} is not traded in this challenge")
        # RuleViolation is turned into a response INSIDE the session, so the rule break is committed
        try:
            p = await engine.get_participant(s, ch, user)
            side = str(body.get("side", "")).upper() if action == "order" else "STOP"
            if side not in {"BUY", "SELL", "STOP"}:
                raise ChallengeError("side must be BUY or SELL")
            stop = _pos(body.get("stop"), "stop", required=False)
            usd = _pos(body.get("usd"), "usd", required=side == "BUY")
            qty = _pos(body.get("qty"), "qty", required=False)
            q = await _quote(fn, symbol)
            if side == "BUY":
                o = await engine.buy(s, ch, p, symbol, q, usd=usd, stop=stop)
                result = {"side": "BUY", "qty": o.qty, "price": o.price, "fee": o.fee_usd}
            elif side == "SELL":
                o, events = await engine.sell(s, ch, p, symbol, q, qty=qty)
                result = {"side": "SELL", "qty": o.qty, "price": o.price, "fee": o.fee_usd, "pnl": o.pnl,
                          "events": [ev.detail for ev in events]}
            else:
                if stop is None:
                    raise ChallengeError("stop is required")
                await engine.set_stop(ch, p, symbol, stop, q)
                result = {"stop": stop}
            return web.json_response({"ok": True, "cash": p.cash, **result})
        except RuleViolation as exc:
            return web.json_response({"error": str(exc), "rule": True}, status=400)
        except ChallengeError as exc:
            return web.json_response({"error": str(exc), "rule": False}, status=400)


async def order(request: web.Request) -> web.Response:
    return await _trade(request, "order")


async def stop(request: web.Request) -> web.Response:
    return await _trade(request, "stop")


def build_app(bot_token: str, quotes_fn: QuoteFn | None = None) -> web.Application:
    app = web.Application(middlewares=[security_headers], client_max_size=16 * 1024)
    app[BOT_TOKEN] = bot_token
    app[QUOTES] = quotes_fn
    app[THROTTLE] = {}
    r = app.router
    r.add_get("/", index)
    r.add_get("/app.js", app_js)
    r.add_get("/api/challenges", challenges)
    r.add_get("/api/challenges/{cid}/leaderboard", leaderboard)
    r.add_get("/api/quotes", quotes)
    r.add_get("/api/session", session)
    r.add_get("/api/me", me)
    r.add_post("/api/invoice", invoice)
    r.add_post("/api/join", join)
    r.add_post("/api/max_buy", max_buy)
    r.add_post("/api/order", order)
    r.add_post("/api/stop", stop)
    return app


async def start_site(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, os.getenv("MINIAPP_HOST", "127.0.0.1"), int(os.getenv("MINIAPP_PORT", "8080"))).start()
    return runner


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    from config import get_settings
    from database.connection import init_db

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (needed to verify Mini App users)")

    async def start() -> web.Application:
        await init_db(get_settings().database_url)
        return build_app(token)          # read-only: trading needs the bot process (python -m tgbot)

    web.run_app(start(), host=os.getenv("MINIAPP_HOST", "127.0.0.1"), port=int(os.getenv("MINIAPP_PORT", "8080")))
