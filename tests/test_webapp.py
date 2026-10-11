import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from competition import engine
from competition.engine import Quote
from database.connection import dispose_engine, get_session, init_db
from database.models import Challenge
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
    assert other == {"joined": False}
    assert (await client.get("/")).status == 200 and (await client.get("/app.js")).status == 200
