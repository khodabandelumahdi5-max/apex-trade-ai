import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from billing import stars
from database.connection import dispose_engine, get_session, init_db
from database.models import StarPayment, User
from tgbot import service as svc
from webapp.auth import sign_init_data
from webapp.server import build_app

TOKEN = "1:stars"


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("STARS_PRICE", "250")
    monkeypatch.setenv("SUBSCRIPTION_DAYS", "30")
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/stars.db")
    yield
    await dispose_engine()


@pytest.mark.asyncio
async def test_checkout_validation_and_idempotent_recording(db):
    async with get_session() as s:
        u = await svc.get_or_create_user(s, 1001, "Ana")
        other = await svc.get_or_create_user(s, 2002, "Bob")
        payload = stars.make_payload(u)
        assert stars.invoice_args(u)["currency"] == "XTR" and stars.invoice_args(u)["prices"][0].amount == 250
        assert await stars.validate_checkout(s, payload, 1001, "XTR", 250) is None
        assert "another account" in await stars.validate_checkout(s, payload, 2002, "XTR", 250)
        assert "not valid" in await stars.validate_checkout(s, payload, 1001, "USD", 250)
        assert "not valid" in await stars.validate_checkout(s, "sub:x", 1001, "XTR", 250)
        assert "price" in await stars.validate_checkout(s, payload, 1001, "XTR", 1)        # tampered amount
        user, new = await stars.record_payment(s, payload, 1001, "XTR", 250, "charge-1")
        assert new and user.subscription_active
        first = user.subscription_expires_at
        assert await stars.record_payment(s, payload, 1001, "XTR", 250, "charge-1") == (None, False)  # duplicate update
        assert user.subscription_expires_at == first
        with pytest.raises(ValueError):
            await stars.record_payment(s, payload, 2002, "XTR", 250, "charge-2")              # wrong payer
        p = (await s.execute(select(StarPayment))).scalar_one()
        stars.apply_refund(user, p)
        assert not user.subscription_active and p.refunded_at is not None
        assert other.subscription_active is False


@pytest.mark.asyncio
async def test_price_change_invalidates_old_invoices(db, monkeypatch):
    async with get_session() as s:
        u = await svc.get_or_create_user(s, 1, "x")
        payload = stars.make_payload(u)
        monkeypatch.setenv("STARS_PRICE", "300")
        assert "price" in await stars.validate_checkout(s, payload, 1, "XTR", 250)


def _tg(uid):
    return {"X-Telegram-Init-Data": sign_init_data({"auth_date": str(int(time.time())),
                                                    "user": json.dumps({"id": uid, "first_name": "x"})}, TOKEN)}


@pytest.mark.asyncio
async def test_stars_api_and_admin_refund(db, monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "9")
    links, refunds = [], []

    async def link(uid):
        links.append(uid)
        return "https://t.me/$invoice"

    async def refund(tg_id, charge):
        refunds.append((tg_id, charge))
    c = TestClient(TestServer(build_app(TOKEN, None, stars_link=link, refund=refund)))
    await c.start_server()
    try:
        sess = await (await c.get("/api/session", headers=_tg(55))).json()
        assert sess["stars"] == 250 and sess["days"] == 30
        r = await c.post("/api/stars_invoice", json={}, headers=_tg(55))
        assert (await r.json())["link"].startswith("https://t.me/") and len(links) == 1
        async with get_session() as s:
            u = (await s.execute(select(User).where(User.telegram_id == 55))).scalar_one()
            await stars.record_payment(s, stars.make_payload(u), 55, "XTR", 250, "ch-9")
        await c.get("/api/session", headers=_tg(9))
        admin = _tg(9)
        st = await (await c.get("/api/admin/stats", headers=admin)).json()
        assert st["stars_total"] == 250
        rows = await (await c.get("/api/admin/stars", headers=admin)).json()
        assert (await c.post(f"/api/admin/stars/{rows[0]['id']}/refund", json={}, headers=admin)).status == 200
        assert refunds == [(55, "ch-9")]
        await asyncio.sleep(0.55)
        assert (await c.post(f"/api/admin/stars/{rows[0]['id']}/refund", json={}, headers=admin)).status == 409
        assert (await (await c.get("/api/session", headers=_tg(55))).json())["subscribed"] is False
    finally:
        await c.close()
