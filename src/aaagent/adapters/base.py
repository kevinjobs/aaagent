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

    async def health_check(self) -> bool:
        """Return True if the adapter is in a healthy state.

        Override in subclasses to perform custom checks (token validity,
        connection liveness, etc.). Default returns True.
        """
        return True
