from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger("aaagent.bus")

Handler = Callable[[Any], Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    async def emit(self, event: str, data: Any = None) -> None:
        handlers = list(self._handlers.get(event, []))
        if not handlers:
            return
        coros = [self._safe_call(event, h, data) for h in handlers]
        await asyncio.gather(*coros, return_exceptions=True)

    async def _safe_call(self, event: str, handler: Handler, data: Any) -> None:
        try:
            await handler(data)
        except Exception:
            logger.exception("Handler for event '%s' raised", event)