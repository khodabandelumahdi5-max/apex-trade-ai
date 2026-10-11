from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from billing.tron import USDT_TRC20_CONTRACT
from competition import engine
from competition.engine import Quote
from database.connection import dispose_engine, get_session, init_db
from database.models import Challenge, ChallengeParticipant, Invoice
from tgbot import service as svc


@pytest_asyncio.fixture
async def db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/bot.db")
    yield
    await dispose_engine()


async def _quotes(sym):
    return Quote(99.9, 100.0)


@pytest.mark.asyncio
async def test_register_is_idempotent_and_private(db):
    async with get_session() as s:
        u1 = await svc.get_or_create_user(s, 555, "<Ali>")
        u2 = await svc.get_or_create_user(s, 555, "<Ali>")
        assert u1.id == u2.id and u1.email == "tg555@telegram.invalid" and not u1.subscription_active
        assert engine.public_name(u1) == "<Ali>"
        assert "&lt;" in svc.e(engine.public_name(u1))                        # HTML is escaped in messages
        u1.display_name = None
        assert engine.public_name(u1) == f"trader#{u1.id}"                    # never the Telegram id


@pytest.mark.asyncio
async def test_invoice_is_reused_not_spammed(db, monkeypatch):
    monkeypatch.setenv("TRON_RECEIVE_ADDRESS", USDT_TRC20_CONTRACT)
    async with get_session() as s:
        u = await svc.get_or_create_user(s, 1, "a")
        t1 = await svc.invoice_text(s, u)
        t2 = await svc.invoice_text(s, u)
        assert t1 == t2 or t1.split("<code>")[1] == t2.split("<code>")[1]
        assert len((await s.execute(select(Invoice))).scalars().all()) == 1
        assert "TRC20" in t1 and "exactly" in t1


@pytest.mark.asyncio
async def test_trade_commands_end_to_end(db):
    async with get_session() as s:
        u = await svc.get_or_create_user(s, 7, "Sara")
        u.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        now = datetime.now(timezone.utc)
        ch = Challenge(title="21", symbols="SOL", start_time=now - timedelta(hours=1), end_time=now + timedelta(days=21))
        s.add(ch)
        await s.flush()
        assert "join the challenge first" in await svc.trade_command(s, u, "buy", [str(ch.id), "SOL", "500", "95"], _quotes)
        await engine.join(s, ch, u)
        assert "Usage" in await svc.trade_command(s, u, "buy", [str(ch.id), "SOL", "500"], _quotes)
        assert "must be a number" in await svc.trade_command(s, u, "buy", [str(ch.id), "SOL", "abc", "95"], _quotes)
        assert "positive" in await svc.trade_command(s, u, "buy", [str(ch.id), "SOL", "-5", "95"], _quotes)
        assert "positive" in await svc.trade_command(s, u, "buy", [str(ch.id), "SOL", "nan", "95"], _quotes)
        assert "⛔ Rule" in await svc.trade_command(s, u, "buy", [str(ch.id), "SOL", "9000", "95"], _quotes)
        ok = await svc.trade_command(s, u, "buy", [str(ch.id), "sol", "1,000", "95"], _quotes)
        assert ok.startswith("✅ Bought")
        assert "raised" in await svc.trade_command(s, u, "stop", [str(ch.id), "SOL", "96"], _quotes)
        assert "Sold" in await svc.trade_command(s, u, "sell", [str(ch.id), "SOL"], _quotes)
        p = (await s.execute(select(ChallengeParticipant))).scalar_one()
        assert p.rule_breaks == 1 and p.holdings == {}
        assert "Discipline 90/100" in svc.participant_text(ch, p)


def test_event_texts_are_escaped():
    ev = engine.Event("disqualified", 1, 1, 1, detail="<b>x</b>")
    assert "&lt;b&gt;" in svc.event_text(ev)
