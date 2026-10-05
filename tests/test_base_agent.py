import pytest

from agents.base_agent import BaseAgent
from core.state import AgentHealth


class Probe(BaseAgent[str]):
    name = "probe"

    def __init__(self):
        super().__init__(retries=0)
        self.beats, self.mode = [], "warmup"

    async def _beat(self, latency_ms, detail=None):
        self.beats.append((self.health.value, detail))

    async def process(self, **_):
        if self.mode == "warmup":
            await self.set_health(AgentHealth.HEALTHY, "warming up 3/10 min")
        elif self.mode == "degraded":
            await self.set_health(AgentHealth.DEGRADED, "no whale wallets")
        return "x"


@pytest.mark.asyncio
async def test_warmup_note_is_replaced_once_warm():
    a = Probe()
    await a.run()
    assert a.beats[-1] == ("HEALTHY", "warming up 3/10 min")
    a.mode = "normal"
    await a.run()
    assert a.beats[-1] == ("HEALTHY", "ok")           # stale "warming up" no longer sticks


@pytest.mark.asyncio
async def test_degraded_status_survives_the_run():
    a = Probe()
    a.mode = "degraded"
    await a.run()
    assert a.beats[-1] == ("DEGRADED", "no whale wallets")
    a.mode = "normal"
    await a.run()
    assert a.beats[-1] == ("HEALTHY", "ok")
