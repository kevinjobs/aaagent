from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from aaagent.core.bus import EventBus
from aaagent.core.message import Message


class IMAdapter(ABC):
    name: str = ""

    def __init__(self, config: dict[str, Any], bus: EventBus) -> None:
        self.config = config
        self.bus = bus

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, msg: Message) -> None: ...

    def _emit_received(self, msg: Message) -> None:
        import asyncio
        asyncio.create_task(self.bus.emit("message_received", msg))
