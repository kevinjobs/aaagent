from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine


Handler = Callable[[Any], Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    async def emit(self, event: str, data: Any = None) -> None:
        for handler in self._handlers.get(event, []):
            try:
                await handler(data)
            except Exception:
                import traceback
                traceback.print_exc()
