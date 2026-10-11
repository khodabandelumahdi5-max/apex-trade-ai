"""Admin API for the panel in the app (/api/admin/*). Every handler checks accounts.is_admin and every change is
written to the AdminAction audit log in the same transaction."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from aiohttp import web
from sqlalchemy import func, or_, select

from billing.service import UNITS, extend_subscription
from competition import engine
from database.connection import get_session
from billing import stars as stars_billing
from database.models import (AdminAction, Challenge, ChallengeParticipant, Invoice, InvoiceStatus, StarPayment, User)
from tgbot import service as svc
from webapp import accounts
from webapp.server import QUOTES, REFUND, _bad, _body, _int, _iso, _quote, _throttle, current_user

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}$")
TX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


async def _admin(request: web.Request, s) -> User:
    user = await current_user(request, s, create=False)
    if not accounts.is_admin(user):
        raise _bad("admins only", status=403)
    return user


def _log(s, admin: User, action: str, target: object = "", **details) -> None:
    s.add(AdminAction(admin_user_id=admin.id, action=action, target=str(target), details=details,
                      created_at=datetime.now(timezone.utc)))


def _num(body: dict, key: str, lo: float, hi: float, default: float | None = None, integer: bool = False) -> float:
    raw = body.get(key, default)
    if raw is None or isinstance(raw, bool):
        raise _bad(f"{key} is required")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise _bad(f"{key} must be a number") from None
    if not (lo <= v <= hi) or (integer and v != int(v)):
        raise _bad(f"{key} must be {'a whole number ' if integer else ''}between {lo:g} and {hi:g}")
    return int(v) if integer else v


def _user_json(u: User) -> dict:
    return {"id": u.id, "name": engine.public_name(u), "via": "telegram" if u.telegram_id else "email",
            "email": None if u.telegram_id or u.email.endswith(".invalid") else u.email,
            "subscribed": u.subscription_active, "expires": _iso(u.subscription_expires_at),
            "active": u.is_active, "created": _iso(u.created_at), "admin": accounts.is_admin(u)}


# ------------------------------------------------------------------------------------------------- overview
async def stats(request: web.Request) -> web.Response:
    async with get_session() as s:
        await _admin(request, s)
        now = datetime.now(timezone.utc)
        users = (await s.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
        paid = (await s.execute(select(Invoice).where(Invoice.status == InvoiceStatus.PAID))).scalars().all()
        month = [i for i in paid if i.paid_at and engine._aware(i.paid_at) >= now - timedelta(days=30)]
        pending = (await s.execute(select(func.count()).select_from(Invoice)
                                   .where(Invoice.status == InvoiceStatus.PENDING))).scalar()
        star_rows = (await s.execute(select(StarPayment).where(StarPayment.refunded_at.is_(None)))).scalars().all()
        chs = (await s.execute(select(Challenge))).scalars().all()
        parts = (await s.execute(select(ChallengeParticipant))).scalars().all()
        return web.json_response({
            "users": len(users), "telegram_users": sum(1 for u in users if u.telegram_id),
            "email_users": sum(1 for u in users if not u.telegram_id),
            "active_subscriptions": sum(1 for u in users if u.subscription_active),
            "revenue_usdt": sum(i.amount_units for i in paid) / UNITS,
            "revenue_30d_usdt": sum(i.amount_units for i in month) / UNITS,
            "paid_invoices": len(paid), "pending_invoices": pending,
            "stars_total": sum(p.stars for p in star_rows),
            "stars_30d": sum(p.stars for p in star_rows if engine._aware(p.created_at) >= now - timedelta(days=30)),
            "live_challenges": sum(1 for c in chs if not c.finalized and engine._aware(c.end_time) > now),
            "participants": len(parts), "disqualified": sum(1 for p in parts if p.status == "DISQUALIFIED")})


# ------------------------------------------------------------------------------------------------- users
async def users(request: web.Request) -> web.Response:
    q = (request.query.get("q") or "").strip().lower()[:64]
    async with get_session() as s:
        await _admin(request, s)
        stmt = select(User).order_by(User.created_at.desc()).limit(100)
        if q:
            like = f"%{q}%"
            conds = [func.lower(User.email).like(like), func.lower(User.display_name).like(like)]
            if q.isdigit():
                conds += [User.id == int(q), User.telegram_id == int(q)]
            stmt = stmt.where(or_(*conds))
        return web.json_response([_user_json(u) for u in (await s.execute(stmt)).scalars()])


async def user_subscription(request: web.Request) -> web.Response:
    body = await _body(request)
    async with svc.LOCK, get_session() as s:
        admin = await _admin(request, s)
        _throttle(request, f"admin{admin.id}")
        u = await s.get(User, _int(request.match_info.get("uid"), "user"))
        if u is None:
            raise _bad("no such user", status=404)
        days = _num(body, "days", -365, 365, integer=True)
        before = u.subscription_expires_at
        if days > 0:
            extend_subscription(u, days)
        else:
            u.subscription_expires_at = engine._aware(u.subscription_expires_at) + timedelta(days=days)
        _log(s, admin, "subscription", u.id, days=days, before=_iso(before), after=_iso(u.subscription_expires_at),
             note=str(body.get("note", ""))[:200])
        return web.json_response(_user_json(u))


async def user_active(request: web.Request) -> web.Response:
    body = await _body(request)
    async with svc.LOCK, get_session() as s:
        admin = await _admin(request, s)
        _throttle(request, f"admin{admin.id}")
        u = await s.get(User, _int(request.match_info.get("uid"), "user"))
        if u is None:
            raise _bad("no such user", status=404)
        if u.id == admin.id:
            raise _bad("you can't disable your own account")
        active = body.get("active")
        if not isinstance(active, bool):
            raise _bad("active must be true or false")
        u.is_active = active
        if not active:
            from sqlalchemy import delete

            from database.models import AuthToken
            await s.execute(delete(AuthToken).where(AuthToken.user_id == u.id))   # sign out everywhere
        _log(s, admin, "enable" if active else "disable", u.id, reason=str(body.get("reason", ""))[:200])
        return web.json_response(_user_json(u))


# ------------------------------------------------------------------------------------------------- payments
async def invoices(request: web.Request) -> web.Response:
    status = (request.query.get("status") or "").upper()
    async with get_session() as s:
        await _admin(request, s)
        stmt = select(Invoice, User).join(User, User.id == Invoice.user_id).order_by(Invoice.created_at.desc()).limit(100)
        if status in InvoiceStatus.__members__:
            stmt = stmt.where(Invoice.status == InvoiceStatus[status])
        rows = (await s.execute(stmt)).all()
        return web.json_response([{"id": i.id, "user": engine.public_name(u), "user_id": u.id,
                                   "amount": i.amount_usdt, "days": i.days, "status": i.status.value,
                                   "created": _iso(i.created_at), "expires": _iso(i.expires_at),
                                   "paid": _iso(i.paid_at), "tx": i.tx_id} for i, u in rows])


async def invoice_mark_paid(request: web.Request) -> web.Response:
    """For a payment the matcher could not attribute (wrong amount, after expiry): the admin checked it on
    Tronscan and attaches the transaction. The tx id stays unique, so one transfer can't pay twice."""
    body = await _body(request)
    async with svc.LOCK, get_session() as s:
        admin = await _admin(request, s)
        _throttle(request, f"admin{admin.id}")
        inv = await s.get(Invoice, _int(request.match_info.get("iid"), "invoice"))
        if inv is None:
            raise _bad("no such invoice", status=404)
        if inv.status == InvoiceStatus.PAID:
            raise _bad("invoice is already paid", status=409)
        tx = str(body.get("tx", "")).strip().lower()
        if not TX_RE.match(tx):
            raise _bad("tx must be the 64-character transaction hash from Tronscan")
        if (await s.execute(select(Invoice).where(Invoice.tx_id == tx))).scalar():
            raise _bad("this transaction already paid another invoice", status=409)
        user = await s.get(User, inv.user_id)
        inv.status, inv.tx_id, inv.paid_at = InvoiceStatus.PAID, tx, datetime.now(timezone.utc)
        extend_subscription(user, inv.days)
        _log(s, admin, "mark_paid", inv.id, tx=tx, user=user.id)
        return web.json_response({"ok": True})


async def star_payments(request: web.Request) -> web.Response:
    async with get_session() as s:
        await _admin(request, s)
        rows = (await s.execute(select(StarPayment, User).join(User, User.id == StarPayment.user_id)
                                .order_by(StarPayment.created_at.desc()).limit(100))).all()
        return web.json_response([{"id": p.id, "user": engine.public_name(u), "user_id": u.id, "stars": p.stars,
                                   "days": p.days, "created": _iso(p.created_at), "refunded": _iso(p.refunded_at)}
                                  for p, u in rows])


async def star_refund(request: web.Request) -> web.Response:
    """Refund through Telegram first; only when Telegram confirms are the days taken back."""
    fn = request.app[REFUND]
    if fn is None:
        raise _bad("refunds need the bot process (python -m tgbot)", status=503)
    async with svc.LOCK, get_session() as s:
        admin = await _admin(request, s)
        _throttle(request, f"admin{admin.id}")
        p = await s.get(StarPayment, _int(request.match_info.get("pid"), "payment"))
        if p is None:
            raise _bad("no such payment", status=404)
        if p.refunded_at is not None:
            raise _bad("already refunded", status=409)
        try:
            await fn(p.telegram_id, p.charge_id)
        except Exception as exc:
            raise _bad(f"Telegram refused the refund: {exc}") from None
        user = await s.get(User, p.user_id)
        stars_billing.apply_refund(user, p)
        _log(s, admin, "stars_refund", p.id, stars=p.stars, user=user.id)
        return web.json_response({"ok": True})


# ------------------------------------------------------------------------------------------------- challenges
async def challenges(request: web.Request) -> web.Response:
    async with get_session() as s:
        await _admin(request, s)
        rows = (await s.execute(select(Challenge).order_by(Challenge.start_time.desc()))).scalars().all()
        counts = dict((await s.execute(select(ChallengeParticipant.challenge_id, func.count())
                                       .group_by(ChallengeParticipant.challenge_id))).all())
        return web.json_response([{"id": c.id, "title": c.title, "symbols": c.symbol_list, "prize": c.prize_text,
                                   "start": _iso(c.start_time), "end": _iso(c.end_time), "final": c.finalized,
                                   "participants": counts.get(c.id, 0)} for c in rows])


async def challenge_create(request: web.Request) -> web.Response:
    body = await _body(request)
    title = str(body.get("title", "")).strip()
    if not 3 <= len(title) <= 120:
        raise _bad("title must be 3–120 characters")
    raw_symbols = body.get("symbols", "")
    symbols = [x.strip().upper() for x in (raw_symbols if isinstance(raw_symbols, list) else str(raw_symbols).split(","))
               if str(x).strip()]
    if not 1 <= len(symbols) <= 10 or not all(SYMBOL_RE.match(x) for x in symbols) or len(set(symbols)) != len(symbols):
        raise _bad("symbols: 1–10 distinct tickers like SOL,JUP")
    days = _num(body, "days", 1, 90, 21, integer=True)
    start_in = _num(body, "start_in_hours", 0, 24 * 30, 0)
    rules = {"starting_balance": _num(body, "starting_balance", 100, 10_000_000, 10_000),
             "fee_bps": _num(body, "fee_bps", 0, 100, 10),
             "max_risk_per_trade_pct": _num(body, "max_risk_per_trade_pct", 0, 10, 1),
             "daily_loss_limit_pct": _num(body, "daily_loss_limit_pct", 0, 50, 3),
             "max_drawdown_pct": _num(body, "max_drawdown_pct", 0, 90, 10),
             "max_consecutive_losses": _num(body, "max_consecutive_losses", 0, 20, 3, integer=True),
             "cooldown_hours": _num(body, "cooldown_hours", 0, 168, 24),
             "max_entries_per_day": _num(body, "max_entries_per_day", 0, 100, 5, integer=True)}
    prize = str(body.get("prize", "")).strip()[:255]
    fn = request.app[QUOTES]
    if fn is not None:                                   # make sure every symbol really trades on MEXC
        for sym in symbols:
            try:
                await _quote(fn, sym)
            except Exception:
                raise _bad(f"{sym}/USDT has no MEXC price") from None
    async with svc.LOCK, get_session() as s:
        admin = await _admin(request, s)
        _throttle(request, f"admin{admin.id}")
        start = datetime.now(timezone.utc) + timedelta(hours=start_in)
        ch = Challenge(title=title, symbols=",".join(symbols), prize_text=prize, start_time=start,
                       end_time=start + timedelta(days=days), **rules)
        s.add(ch)
        await s.flush()
        _log(s, admin, "challenge_create", ch.id, title=title, symbols=symbols, days=days, **rules)
        return web.json_response({"id": ch.id})


async def challenge_update(request: web.Request) -> web.Response:
    """Change the prize text, or end the challenge now (the next tick finalises it)."""
    body = await _body(request)
    async with svc.LOCK, get_session() as s:
        admin = await _admin(request, s)
        _throttle(request, f"admin{admin.id}")
        ch = await s.get(Challenge, _int(request.match_info.get("cid")))
        if ch is None:
            raise _bad("no such challenge", status=404)
        if ch.finalized:
            raise _bad("challenge is final and can't be changed", status=409)
        if "prize" in body:
            ch.prize_text = str(body["prize"]).strip()[:255]
            _log(s, admin, "challenge_prize", ch.id, prize=ch.prize_text)
        if body.get("end_now") is True:
            now = datetime.now(timezone.utc)
            if engine._aware(ch.end_time) > now:
                ch.end_time = now
                if engine._aware(ch.start_time) > now:
                    ch.start_time = now
            _log(s, admin, "challenge_end", ch.id)
        return web.json_response({"ok": True})


async def audit(request: web.Request) -> web.Response:
    async with get_session() as s:
        await _admin(request, s)
        rows = (await s.execute(select(AdminAction, User).join(User, User.id == AdminAction.admin_user_id, isouter=True)
                                .order_by(AdminAction.created_at.desc()).limit(200))).all()
        return web.json_response([{"at": _iso(a.created_at), "admin": engine.public_name(u) if u else "?",
                                   "action": a.action, "target": a.target, "details": a.details} for a, u in rows])


def add_routes(r: web.UrlDispatcher) -> None:
    r.add_get("/api/admin/stats", stats)
    r.add_get("/api/admin/users", users)
    r.add_post("/api/admin/users/{uid}/subscription", user_subscription)
    r.add_post("/api/admin/users/{uid}/active", user_active)
    r.add_get("/api/admin/invoices", invoices)
    r.add_post("/api/admin/invoices/{iid}/mark_paid", invoice_mark_paid)
    r.add_get("/api/admin/stars", star_payments)
    r.add_post("/api/admin/stars/{pid}/refund", star_refund)
    r.add_get("/api/admin/challenges", challenges)
    r.add_post("/api/admin/challenges", challenge_create)
    r.add_post("/api/admin/challenges/{cid}", challenge_update)
    r.add_get("/api/admin/audit", audit)
