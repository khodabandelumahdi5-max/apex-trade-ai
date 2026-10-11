import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from competition.engine import Quote
from database.connection import dispose_engine, get_session, init_db
from database.models import AdminAction, ChallengeParticipant, Invoice, InvoiceStatus, User
from webapp import accounts
from webapp.auth import sign_init_data
from webapp.server import build_app

TOKEN = "1:admin-test"
ADMIN_TG = 777


def _tg(uid):
    return {"X-Telegram-Init-Data": sign_init_data({"auth_date": str(int(time.time())),
                                                    "user": json.dumps({"id": uid, "first_name": f"u{uid}"})}, TOKEN)}


@pytest_asyncio.fixture
async def c(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", str(ADMIN_TG))
    monkeypatch.setenv("TRON_RECEIVE_ADDRESS", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    accounts._fails.clear()
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/adm.db")

    async def quotes(sym):
        if sym == "NOPE":
            raise RuntimeError("no market")
        return Quote(99.9, 100.0)
    client = TestClient(TestServer(build_app(TOKEN, quotes)))
    await client.start_server()
    yield client
    await client.close()
    await dispose_engine()


async def _post(c, path, body, headers, wait=True):
    if wait:
        await asyncio.sleep(0.55)                 # per-user write throttle
    return await c.post(path, json=body, headers=headers)


@pytest.mark.asyncio
async def test_register_login_logout_and_lockout(c):
    r = await c.post("/api/auth/register", json={"email": "a@b.co", "password": "longpassword1"})
    assert r.status == 400 and "terms" in (await r.json())["error"]
    r = await _post(c, "/api/auth/register", {"email": "A@B.co", "password": "short", "accept_terms": True}, {})
    assert r.status == 400
    r = await _post(c, "/api/auth/register", {"email": "A@B.co", "password": "longpassword1", "accept_terms": True}, {})
    token = (await r.json())["token"]
    sess = await (await c.get("/api/session", headers={"Authorization": f"Bearer {token}"})).json()
    assert sess["via"] == "email" and sess["email"] == "a@b.co" and sess["admin"] is False
    r = await _post(c, "/api/auth/register", {"email": "a@b.co", "password": "longpassword1", "accept_terms": True}, {})
    assert r.status == 400 and "exists" in (await r.json())["error"]
    async with get_session() as s:                # the token itself is never stored
        assert token not in json.dumps([t.token_hash for t in (await s.execute(select(accounts.AuthToken))).scalars()])
    await c.post("/api/auth/logout", json={}, headers={"Authorization": f"Bearer {token}"})
    assert (await c.get("/api/session", headers={"Authorization": f"Bearer {token}"})).status == 401
    for _ in range(5):
        r = await _post(c, "/api/auth/login", {"email": "a@b.co", "password": "wrong-password"}, {})
        assert r.status == 401
    r = await _post(c, "/api/auth/login", {"email": "a@b.co", "password": "longpassword1"}, {})
    assert r.status == 401 and "too many" in (await r.json())["error"]     # locked even with the right password
    accounts._fails.clear()
    r = await _post(c, "/api/auth/login", {"email": "a@b.co", "password": "longpassword1"}, {})
    assert r.status == 200
    assert (await c.get("/api/session", headers={"Authorization": "Bearer forged"})).status == 401


@pytest.mark.asyncio
async def test_account_deletion_removes_personal_data(c):
    r = await _post(c, "/api/auth/register", {"email": "del@x.co", "password": "longpassword1", "accept_terms": True}, {})
    h = {"Authorization": "Bearer " + (await r.json())["token"]}
    await _post(c, "/api/invoice", {}, h)
    r = await _post(c, "/api/account/delete", {"password": "nope-nope-nope"}, h)
    assert r.status == 401
    assert (await _post(c, "/api/account/delete", {"password": "longpassword1"}, h)).status == 200
    assert (await c.get("/api/session", headers=h)).status == 401
    async with get_session() as s:
        u = (await s.execute(select(User))).scalar_one()
        assert u.email.endswith("@deleted.invalid") and not u.is_active and u.hashed_password == "!"
        assert (await s.execute(select(Invoice))).scalar_one().user_id == u.id      # invoice kept, anonymised owner
    # telegram users confirm with DELETE
    await c.get("/api/session", headers=_tg(5))
    assert (await _post(c, "/api/account/delete", {"confirm": "yes"}, _tg(5))).status == 400
    assert (await _post(c, "/api/account/delete", {"confirm": "DELETE"}, _tg(5))).status == 200
    sess = await (await c.get("/api/session", headers=_tg(5))).json()                # opening again = fresh account
    assert sess["subscribed"] is False


@pytest.mark.asyncio
async def test_admin_requires_admin(c):
    await c.get("/api/session", headers=_tg(1))
    assert (await c.get("/api/admin/stats", headers=_tg(1))).status == 403
    assert (await c.get("/api/admin/stats")).status == 401
    r = await _post(c, "/api/admin/challenges", {"title": "x" * 10, "symbols": "SOL"}, _tg(1))
    assert r.status == 403


@pytest.mark.asyncio
async def test_admin_flow(c):
    A = _tg(ADMIN_TG)
    assert (await (await c.get("/api/session", headers=A)).json())["admin"] is True
    r = await _post(c, "/api/admin/challenges", {"title": "Bad", "symbols": "SOL,NOPE"}, A)
    assert r.status == 400 and "NOPE" in (await r.json())["error"]
    r = await _post(c, "/api/admin/challenges", {"title": "AlphaDuel 21", "symbols": "sol, jup", "days": 21,
                                                  "prize": "100 USDT", "max_risk_per_trade_pct": 50}, A)
    assert r.status == 400                                                       # rule out of range
    r = await _post(c, "/api/admin/challenges", {"title": "AlphaDuel 21", "symbols": "sol, jup", "days": "21",
                                                  "prize": "100 USDT"}, A)
    cid = (await r.json())["id"]
    lst = await (await c.get("/api/admin/challenges", headers=A)).json()
    assert lst[0]["symbols"] == ["SOL", "JUP"] and lst[0]["participants"] == 0

    # a user pays the wrong amount → admin attaches the tx manually
    U = _tg(42)
    await c.get("/api/session", headers=U)
    inv = await (await _post(c, "/api/invoice", {}, U)).json()
    users = await (await c.get("/api/admin/users?q=u42", headers=A)).json()
    uid = users[0]["id"]
    assert users[0]["subscribed"] is False
    pending = await (await c.get("/api/admin/invoices?status=PENDING", headers=A)).json()
    iid = pending[0]["id"]
    assert pending[0]["amount"] == inv["amount"]
    r = await _post(c, f"/api/admin/invoices/{iid}/mark_paid", {"tx": "abc"}, A)
    assert r.status == 400
    tx = "a" * 64
    assert (await _post(c, f"/api/admin/invoices/{iid}/mark_paid", {"tx": tx}, A)).status == 200
    assert (await _post(c, f"/api/admin/invoices/{iid}/mark_paid", {"tx": tx}, A)).status == 409
    assert (await (await c.get("/api/session", headers=U)).json())["subscribed"] is True
    assert (await _post(c, "/api/join", {"challenge": cid}, U)).status == 200

    # grant/remove days and disable
    r = await (await _post(c, f"/api/admin/users/{uid}/subscription", {"days": -400}, A)).json()
    assert "error" in r
    assert (await _post(c, f"/api/admin/users/{uid}/subscription", {"days": 7}, A)).status == 200
    assert (await _post(c, f"/api/admin/users/{uid}/active", {"active": False}, A)).status == 200
    assert (await c.get("/api/session", headers=U)).status == 403                # disabled user is locked out
    stats = await (await c.get("/api/admin/stats", headers=A)).json()
    assert stats["revenue_usdt"] == float(inv["amount"]) and stats["participants"] == 1
    assert (await _post(c, f"/api/admin/challenges/{cid}", {"end_now": True}, A)).status == 200
    log = await (await c.get("/api/admin/audit", headers=A)).json()
    assert [a["action"] for a in log][:4] == ["challenge_end", "disable", "subscription", "mark_paid"]
    async with get_session() as s:
        assert (await s.execute(select(ChallengeParticipant))).scalar_one().user_id == uid
        assert len((await s.execute(select(AdminAction))).scalars().all()) == 5


@pytest.mark.asyncio
async def test_pwa_files_served(c):
    for path, ctype in [("/manifest.webmanifest", "application/manifest+json"), ("/sw.js", "application/javascript"),
                        ("/icon-512.png", "image/png"), ("/privacy", "text/html"), ("/terms", "text/html"),
                        ("/admin.js", "application/javascript")]:
        r = await c.get(path)
        assert r.status == 200 and (r.content_type == ctype or ("javascript" in ctype and "javascript" in r.content_type)), path
