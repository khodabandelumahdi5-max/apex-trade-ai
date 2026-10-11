from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from billing.service import (UNITS, apply_transfers, create_invoice, expire_invoices, extend_subscription,
                             subscription_allows_trading)
from billing.tron import USDT_TRC20_CONTRACT, Transfer, is_tron_address, parse_transfers
from database.connection import dispose_engine, get_session, init_db
from database.models import Invoice, InvoiceStatus, User

ADDR = USDT_TRC20_CONTRACT          # any valid TRON address works for the tests
NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/billing.db")
    yield
    await dispose_engine()


async def _user(s, email="a@b.co", expires=NOW - timedelta(days=1)):
    u = User(email=email, hashed_password="x", subscription_expires_at=expires)
    s.add(u)
    await s.flush()
    return u


def test_tron_address_checksum():
    assert is_tron_address(USDT_TRC20_CONTRACT)
    assert not is_tron_address(USDT_TRC20_CONTRACT[:-1] + "u")       # one character changed
    assert not is_tron_address("0x" + "a" * 32)


def test_parse_rejects_fake_usdt_and_outgoing():
    def tx(contract, to=ADDR, typ="Transfer", tid="t1"):
        return {"transaction_id": tid, "type": typ, "to": to, "value": "29010000", "block_timestamp": 1_700_000_000_000,
                "token_info": {"address": contract, "symbol": "USDT", "decimals": 6}}
    payload = {"data": [tx(USDT_TRC20_CONTRACT), tx("TFakeUSDTcontractXXXXXXXXXXXXXXXXX", tid="t2"),
                        tx(USDT_TRC20_CONTRACT, to="TSomeoneElse", tid="t3"),
                        tx(USDT_TRC20_CONTRACT, typ="Approval", tid="t4")]}
    got = parse_transfers(payload, ADDR)
    assert [t.tx_id for t in got] == ["t1"] and got[0].amount_units == 29_010_000


@pytest.mark.asyncio
async def test_unique_amounts_and_payment_flow(db):
    async with get_session() as s:
        u1, u2 = await _user(s, "a@b.co"), await _user(s, "c@d.co")
        i1 = await create_invoice(s, u1, ADDR, days=30, base_units=29 * UNITS)
        i2 = await create_invoice(s, u2, ADDR, days=30, base_units=29 * UNITS)
        assert i1.amount_units != i2.amount_units
        assert 29 * UNITS < i1.amount_units < 30 * UNITS and i1.amount_units % (UNITS // 100) == 0
        at = NOW + timedelta(minutes=5)
        transfers = [Transfer("tx-wrong", ADDR, i1.amount_units - 10_000, at),     # 1 cent short
                     Transfer("tx-1", ADDR, i1.amount_units, at)]
        paid = await apply_transfers(s, transfers, ADDR)
        assert [p.id for p in paid] == [i1.id]
        assert u1.subscription_active and not u2.subscription_active
        # the same transaction seen again (next poll) must not extend twice
        before = u1.subscription_expires_at
        assert await apply_transfers(s, transfers, ADDR) == []
        assert u1.subscription_expires_at == before
        # a second invoice with the same amount cannot reuse tx-1 either
        i2.amount_units = i1.amount_units
        assert await apply_transfers(s, transfers, ADDR) == []


@pytest.mark.asyncio
async def test_expiry_and_late_payment(db):
    async with get_session() as s:
        u = await _user(s)
        inv = await create_invoice(s, u, ADDR, ttl_hours=1, base_units=29 * UNITS)
        late = Transfer("tx-late", ADDR, inv.amount_units, NOW + timedelta(hours=2))
        assert await expire_invoices(s, now=NOW + timedelta(hours=2)) == 1
        assert await apply_transfers(s, [late], ADDR) == []
        assert (await s.execute(select(Invoice))).scalar_one().status == InvoiceStatus.EXPIRED
        # the expired invoice's amount stays reserved, so the late payment can't pay someone else's invoice
        nxt = await create_invoice(s, u, ADDR, base_units=29 * UNITS)
        assert nxt.amount_units != inv.amount_units


def test_extend_keeps_remaining_time_and_guard():
    u = User(email="x@y.co", hashed_password="x", is_active=True, subscription_expires_at=NOW + timedelta(days=10))
    extend_subscription(u, 30, now=NOW)
    assert u.subscription_expires_at == NOW + timedelta(days=40)
    assert subscription_allows_trading(u)
    u.subscription_expires_at = NOW - timedelta(days=1)
    assert not subscription_allows_trading(u) and u.is_active       # paused, not banned
    extend_subscription(u, 30, now=NOW)
    assert u.subscription_expires_at == NOW + timedelta(days=30)
