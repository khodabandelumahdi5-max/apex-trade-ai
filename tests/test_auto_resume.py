import pytest
import pytest_asyncio

from config import Settings, WatchToken
from core.coordinator import AUTO_RESUME_AFTER_OK_CYCLES, SwarmCoordinator
from database import repository as repo
from database.connection import dispose_engine, init_db


@pytest_asyncio.fixture
async def coordinator(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    s = Settings(watchlist=[WatchToken(symbol="SOL", mint="So11111111111111111111111111111111111111112")])
    await repo.get_control(1.0, 0.25)
    c = SwarmCoordinator(s)
    yield c
    await c.shutdown()
    await dispose_engine()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["auto: 5 consecutive cycle failures (network/API)", "21 consecutive cycle failures"])
async def test_network_halt_resumes_after_good_cycles(coordinator, reason):
    await repo.update_control(halted=True, reason=reason)
    coordinator._consecutive_ok = AUTO_RESUME_AFTER_OK_CYCLES - 1
    await coordinator._maybe_auto_resume()
    assert (await repo.get_control(1.0, 0.25)).halted
    coordinator._consecutive_ok = AUTO_RESUME_AFTER_OK_CYCLES
    await coordinator._maybe_auto_resume()
    assert not (await repo.get_control(1.0, 0.25)).halted


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Emergency halt from dashboard", "drawdown 11.00% ≥ 10.0%"])
async def test_risk_halts_never_auto_resume(coordinator, reason):
    await repo.update_control(halted=True, reason=reason)
    coordinator._consecutive_ok = 100
    await coordinator._maybe_auto_resume()
    assert (await repo.get_control(1.0, 0.25)).halted
