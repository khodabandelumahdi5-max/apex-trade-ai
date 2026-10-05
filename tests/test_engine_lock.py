from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from database import repository as repo
from database.connection import dispose_engine, get_session, init_db
from database.models import AgentHeartbeat


@pytest_asyncio.fixture
async def db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/lock.db")
    yield
    await dispose_engine()


@pytest.mark.asyncio
async def test_second_engine_is_refused_while_first_is_alive(db):
    assert await repo.claim_engine_lock("laptop:111") is None
    assert await repo.claim_engine_lock("laptop:222") == "laptop:111"
    assert await repo.refresh_engine_lock("laptop:111")
    assert not await repo.refresh_engine_lock("laptop:222")


@pytest.mark.asyncio
async def test_stale_lock_from_a_closed_window_is_taken_over(db):
    assert await repo.claim_engine_lock("laptop:111") is None
    async with get_session() as s:                      # window closed: no refresh for a minute
        row = await s.get(AgentHeartbeat, repo.ENGINE_LOCK)
        row.last_seen = datetime.now(timezone.utc) - timedelta(seconds=60)
    assert await repo.claim_engine_lock("laptop:222") is None
    assert not await repo.refresh_engine_lock("laptop:111")   # the old one would stop itself


@pytest.mark.asyncio
async def test_clean_shutdown_releases_immediately(db):
    assert await repo.claim_engine_lock("laptop:111") is None
    await repo.release_engine_lock("laptop:111")
    assert await repo.claim_engine_lock("laptop:222") is None
