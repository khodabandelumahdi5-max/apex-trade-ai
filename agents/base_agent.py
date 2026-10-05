"""Abstract base class for all swarm agents."""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from loguru import logger

from core.state import AgentHealth
from database import repository as repo

T = TypeVar("T")


class AgentError(RuntimeError):
    pass


class BaseAgent(ABC, Generic[T]):
    """Standardised execution wrapper: timeout, retries with backoff, health + heartbeat."""

    name: str = "base"

    def __init__(self, timeout: float = 90.0, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries
        self.health = AgentHealth.STARTING
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.detail: str | None = None   # status note for the dashboard; reset on every run
        self.log = logger.bind(agent=self.name)

    @abstractmethod
    async def process(self, **inputs: Any) -> T:
        """Agent logic; must be idempotent so it can be retried."""

    async def startup(self) -> None:
        """Optional async initialisation hook."""

    async def shutdown(self) -> None:
        """Optional cleanup hook."""

    async def run(self, **inputs: Any) -> T:
        last_exc: BaseException | None = None
        for attempt in range(1, self.retries + 2):
            started = time.perf_counter()
            self.detail = None
            try:
                result = await asyncio.wait_for(self.process(**inputs), timeout=self.timeout)
                self.consecutive_failures = 0
                # keep a DEGRADED status the agent set during this run (e.g. missing whale config)
                if not (self.health == AgentHealth.DEGRADED and self.detail):
                    self.health = AgentHealth.HEALTHY
                self.last_error = None
                # always overwrite the note, so a finished warm-up does not linger on the dashboard
                await self._beat((time.perf_counter() - started) * 1000, self.detail or "ok")
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                self.log.warning("{} attempt {}/{} failed: {!r}", self.name, attempt, self.retries + 1, exc)
                if attempt <= self.retries and self.is_retryable(exc):
                    await asyncio.sleep(min(10.0, 1.5 * 2 ** (attempt - 1)))
                    continue
                break
        self.consecutive_failures += 1
        self.last_error = repr(last_exc)
        self.health = AgentHealth.FAILED if self.consecutive_failures >= 3 else AgentHealth.DEGRADED
        await self._beat(None)
        raise AgentError(f"{self.name} failed: {last_exc!r}") from last_exc

    def is_retryable(self, exc: Exception) -> bool:
        from exchange_connector import FatalConnectorError
        return not isinstance(exc, (FatalConnectorError, ValueError))

    async def _beat(self, latency_ms: float | None, detail: str | None = None) -> None:
        try:
            await repo.heartbeat(self.name, self.health.value, latency_ms, self.last_error, detail)
        except Exception as exc:
            self.log.error("heartbeat write failed: {}", exc)

    async def set_health(self, health: AgentHealth, detail: str | None = None) -> None:
        self.health, self.detail = health, detail
        await self._beat(None, detail)
