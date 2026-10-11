import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from competition import engine
from competition.engine import Quote
from database.connection import dispose_engine, get_session, init_db
from sqlalchemy import select

from database.models import Challenge, User
from tgbot import service as svc
from webapp.auth import InitDataError, sign_init_data, verify_init_data
from webapp.server import build_app

TOKEN = "123456:TEST-token"


def _init(uid=42, age=0, token=TOKEN):
    return sign_init_data({"auth_date": str(int(time.time()) - age), "query_id": "q",
                           "user": json.dumps({"id": uid, "first_name": "A"})}, token)


def test_init_data_verification():
    assert verify_init_data(_init(), TOKEN)["id"] == 42
    for bad in (_init(token="999:other"), _init(age=2 * 86_400), "", "hash=00&user=%7B%7D"):
        with pytest.raises(InitDataError):
            verify_init_data(bad, TOKEN)
    tampered = _init().replace("%22id%22%3A+42", "%22id%22%3A+43")
    with pytest.raises(InitDataError):
        verify_init_data(tampered, TOKEN)


@pytest_asyncio.fixture
async def client(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/web.db")
    now = datetime.now(timezone.utc)
    async with get_session() as s:
        u = await svc.get_or_create_user(s, 42, "<script>")
        u.subscription_expires_at = now + timedelta(days=30)
        ch = Challenge(title="21-day", symbols="SOL", start_time=now - timedelta(hours=1),
                       end_time=now + timedelta(days=21))
        s.add(ch)
        await s.flush()
        p = await engine.join(s, ch, u)
        await engine.buy(s, ch, p, "SOL", Quote(99.9, 100), usd=500, stop=95)
        await engine.tick(s, ch, {"SOL": Quote(101, 101.1)})
    c = TestClient(TestServer(build_app(TOKEN)))
    await c.start_server()
    yield c
    await c.close()
    await dispose_engine()


@pytest.mark.asyncio
async def test_api(client):
    r = await client.get("/api/challenges")
    assert r.status == 200 and (await r.json())[0]["rules"]["risk_per_trade_pct"] == 1.0
    assert "frame-ancestors" in r.headers["Content-Security-Policy"]
    board = await (await client.get("/api/challenges/1/leaderboard")).json()
    assert board["rows"][0]["name"] == "<script>" and board["rows"][0]["rank"] == 1   # escaped by the page
    assert "42" not in json.dumps(board)                                                # no Telegram ids
    assert (await client.get("/api/challenges/abc/leaderboard")).status == 400
    assert (await client.get("/api/challenges/999/leaderboard")).status == 404
    assert (await client.get("/api/me?challenge=1")).status == 401
    me = await (await client.get("/api/me?challenge=1", headers={"X-Telegram-Init-Data": _init()})).json()
    assert me["joined"] and me["roi"] > 0 and len(me["curve"]) == 1 and me["positions"][0]["stop"] == 95
    other = await (await client.get("/api/me?challenge=1", headers={"X-Telegram-Init-Data": _init(uid=7)})).json()
    assert other["joined"] is False
    assert (await client.get("/")).status == 200 and (await client.get("/app.js")).status == 200


@pytest_asyncio.fixture
async def trading(tmp_path, monkeypatch):
    monkeypatch.setenv("TRON_RECEIVE_ADDRESS", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/trade.db")
    now = datetime.now(timezone.utc)
    async with get_session() as s:
        s.add(Challenge(title="21-day", symbols="SOL", start_time=now - timedelta(hours=1),
                        end_time=now + timedelta(days=21)))

    async def quotes(sym):
        return Quote(99.9, 100.0)
    c = TestClient(TestServer(build_app(TOKEN, quotes)))
    await c.start_server()
    yield c
    await c.close()
    await dispose_engine()


def _h(uid=42):
    return {"X-Telegram-Init-Data": _init(uid)}


@pytest.mark.asyncio
async def test_app_trading_flow(trading):
    c = trading
    assert (await c.post("/api/join", json={"challenge": 1})).status == 401             # no Telegram signature
    sess = await (await c.get("/api/session", headers=_h())).json()
    assert sess["subscribed"] is False and sess["payments"] is True
    r = await c.post("/api/join", json={"challenge": 1}, headers=_h())
    assert r.status == 400 and "subscription" in (await r.json())["error"]
    await asyncio.sleep(0.6)
    inv1 = await (await c.post("/api/invoice", json={}, headers=_h())).json()
    await asyncio.sleep(0.6)                                                              # write throttle
    inv2 = await (await c.post("/api/invoice", json={}, headers=_h())).json()
    assert inv1["amount"] == inv2["amount"] and inv1["network"] == "TRON (TRC20)"
    async with get_session() as s:                                                        # simulate a payment
        u = (await s.execute(select(User))).scalar_one()
        u.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    await asyncio.sleep(0.6)
    assert (await c.post("/api/join", json={"challenge": 1}, headers=_h())).status == 200
    q = await (await c.get("/api/quotes?challenge=1")).json()
    assert q == {"SOL": {"bid": 99.9, "ask": 100.0}}
    mx = await (await c.post("/api/max_buy", json={"challenge": 1, "symbol": "SOL", "stop": 95}, headers=_h())).json()
    assert 1500 < mx["max_usd"] < 2000
    await asyncio.sleep(0.6)
    r = await c.post("/api/order", json={"challenge": 1, "side": "BUY", "symbol": "SOL", "usd": mx["max_usd"] + 50,
                                          "stop": 95}, headers=_h())
    body = await r.json()
    assert r.status == 400 and body["rule"] is True                                       # over the risk limit
    await asyncio.sleep(0.6)
    r = await c.post("/api/order", json={"challenge": 1, "side": "BUY", "symbol": "SOL", "usd": mx["max_usd"],
                                          "stop": 95}, headers=_h())
    assert r.status == 200 and (await r.json())["price"] == 100.0
    me = await (await c.get("/api/me?challenge=1", headers=_h())).json()
    assert me["rule_breaks"] == 1 and me["positions"][0]["risk"] <= me["risk_budget"] + 1e-6   # rule break saved
    await asyncio.sleep(0.6)
    r = await c.post("/api/stop", json={"challenge": 1, "symbol": "SOL", "stop": 90}, headers=_h())
    assert r.status == 400 and (await r.json())["rule"] is True                           # lowering a stop
    await asyncio.sleep(0.6)
    assert (await c.post("/api/stop", json={"challenge": 1, "symbol": "SOL", "stop": 97}, headers=_h())).status == 200
    await asyncio.sleep(0.6)
    r = await c.post("/api/order", json={"challenge": 1, "side": "SELL", "symbol": "SOL"}, headers=_h())
    assert r.status == 200 and (await r.json())["pnl"] < 0                                 # spread + fees


@pytest.mark.asyncio
async def test_app_input_validation_and_throttle(trading):
    c = trading
    bad_bodies = [{"challenge": "x"}, {"challenge": True}, {"challenge": 1, "side": "BUY", "symbol": "SOL",
                  "usd": "NaN", "stop": 95}, {"challenge": 1, "side": "BUY", "symbol": "BTC", "usd": 10, "stop": 1},
                  {"challenge": 1, "side": "HOLD", "symbol": "SOL"}, [1, 2]]
    for i, body in enumerate(bad_bodies):
        r = await c.post("/api/order", json=body, headers=_h(100 + i))
        assert r.status == 400, body
    r1 = await c.post("/api/invoice", json={}, headers=_h(7))
    r2 = await c.post("/api/invoice", json={}, headers=_h(7))
    assert r1.status == 200 and r2.status == 429
    assert (await c.post("/api/order", data="not json", headers=_h(8))).status == 400


@pytest.mark.asyncio
async def test_read_only_mode_refuses_trading(client):
    r = await client.post("/api/order", json={"challenge": 1, "side": "SELL", "symbol": "SOL"}, headers=_h())
    assert r.status == 503
