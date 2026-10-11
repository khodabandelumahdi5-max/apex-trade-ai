"""aiohttp server for the Mini App. Read-only: trading stays in the bot commands, which keeps this surface small.

    python -m webapp            # listens on 127.0.0.1:8080; put Caddy in front for HTTPS (Telegram requires it)

GET /                                  the Mini App page
GET /api/challenges                    live and finished challenges
GET /api/challenges/{id}/leaderboard   ranked traders (public names only)
GET /api/me?challenge={id}             your stats + equity curve (header X-Telegram-Init-Data required)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from sqlalchemy import select

from competition import engine
from database.connection import get_session
from database.models import Challenge, ChallengeEquityPoint, User
from webapp.auth import InitDataError, verify_init_data

STATIC = Path(__file__).parent / "static"
BOT_TOKEN = web.AppKey("bot_token", str)


def _iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _cid(request: web.Request, raw: str | None) -> int:
    if raw is None or not raw.isdigit() or len(raw) > 12:
        raise web.HTTPBadRequest(text="bad challenge id")
    return int(raw)


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


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def challenges(_: web.Request) -> web.Response:
    async with get_session() as s:
        rows = (await s.execute(select(Challenge).order_by(Challenge.start_time.desc()).limit(20))).scalars().all()
        out = [{"id": c.id, "title": c.title, "symbols": c.symbol_list, "prize": c.prize_text,
                "start": _iso(c.start_time), "end": _iso(c.end_time), "final": c.finalized,
                "rules": {"risk_per_trade_pct": c.max_risk_per_trade_pct, "daily_loss_pct": c.daily_loss_limit_pct,
                          "max_drawdown_pct": c.max_drawdown_pct, "losses_before_pause": c.max_consecutive_losses,
                          "pause_hours": c.cooldown_hours, "entries_per_day": c.max_entries_per_day}}
               for c in rows]
    return web.json_response(out)


async def leaderboard(request: web.Request) -> web.Response:
    cid = _cid(request, request.match_info.get("cid"))
    async with get_session() as s:
        ch = await s.get(Challenge, cid)
        if ch is None:
            raise web.HTTPNotFound(text="no such challenge")
        rows = engine.rank(await engine.participants(s, ch))
        ids = [p.user_id for p in rows] or [-1]
        names = {u.id: engine.public_name(u) for u in (await s.execute(select(User).where(User.id.in_(ids)))).scalars()}
        out = [{"rank": p.rank, "name": names.get(p.user_id, "?"), "roi": round(p.roi_pct, 4),
                "discipline": engine.discipline_score(p), "max_dd": round(p.max_drawdown_pct, 4),
                "trades": p.trades_count, "status": p.status} for p in rows[:100]]
    return web.json_response({"challenge": ch.title, "final": ch.finalized, "rows": out})


async def me(request: web.Request) -> web.Response:
    try:
        tg_user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), request.app[BOT_TOKEN])
    except InitDataError as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from None
    cid = _cid(request, request.query.get("challenge"))
    async with get_session() as s:
        user = (await s.execute(select(User).where(User.telegram_id == tg_user["id"]))).scalar()
        ch = await s.get(Challenge, cid)
        if user is None or ch is None:
            return web.json_response({"joined": False})
        try:
            p = await engine.get_participant(s, ch, user)
        except engine.ChallengeError:
            return web.json_response({"joined": False, "subscribed": user.subscription_active})
        points = (await s.execute(select(ChallengeEquityPoint).where(ChallengeEquityPoint.participant_id == p.id)
                                  .order_by(ChallengeEquityPoint.ts))).scalars().all()
        return web.json_response({
            "joined": True, "subscribed": user.subscription_active, "status": p.status, "reason": p.status_reason,
            "rank": p.rank, "roi": p.roi_pct, "equity": p.equity, "cash": p.cash, "max_dd": p.max_drawdown_pct,
            "discipline": engine.discipline_score(p), "wins": p.wins, "losses": p.losses,
            "cooldown_until": _iso(p.cooldown_until) if p.cooldown_until else None,
            "positions": [{"symbol": k, "qty": v["qty"], "avg": v["cost"] / v["qty"], "stop": v["stop"]}
                          for k, v in (p.holdings or {}).items()],
            "curve": [[_iso(pt.ts), round(pt.equity, 2)] for pt in points[-2000:]]})


def build_app(bot_token: str) -> web.Application:
    app = web.Application(middlewares=[security_headers], client_max_size=64 * 1024)
    app[BOT_TOKEN] = bot_token
    app.router.add_get("/", index)
    app.router.add_get("/app.js", lambda _: web.FileResponse(STATIC / "app.js"))
    app.router.add_get("/api/challenges", challenges)
    app.router.add_get("/api/challenges/{cid}/leaderboard", leaderboard)
    app.router.add_get("/api/me", me)
    return app


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    import asyncio

    from config import get_settings
    from database.connection import init_db

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (needed to verify Mini App users)")

    async def start() -> web.Application:
        await init_db(get_settings().database_url)
        return build_app(token)

    web.run_app(start(), host=os.getenv("MINIAPP_HOST", "127.0.0.1"), port=int(os.getenv("MINIAPP_PORT", "8080")))
