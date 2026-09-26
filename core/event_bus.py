"""Minimal in-process async pub/sub used by the coordinator to fan out swarm events."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Callable

from loguru import logger

Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Subscribe to a topic, or to every topic with '*'."""
        self._handlers[topic].append(handler)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        handlers = self._handlers.get(topic, []) + self._handlers.get("*", [])
        results = await asyncio.gather(*(h(topic, payload) for h in handlers), return_exceptions=True)
        for handler, result in zip(handlers, results):
            if isinstance(result, Exception):
                logger.error("event handler {} failed on '{}': {!r}", getattr(handler, "__name__", handler),
                             topic, result)
