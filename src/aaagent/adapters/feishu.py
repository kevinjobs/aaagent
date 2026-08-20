from __future__ import annotations

from typing import Any

from aaagent.adapters.base import IMAdapter
from aaagent.core.bus import EventBus
from aaagent.core.message import Message


class FeishuAdapter(IMAdapter):
    name = "feishu"

    def __init__(self, config: dict[str, Any], bus: EventBus) -> None:
        super().__init__(config, bus)

    async def start(self) -> None:
        raise NotImplementedError("Feishu adapter not yet implemented")

    async def stop(self) -> None:
        pass

    async def send(self, msg: Message) -> None:
        raise NotImplementedError("Feishu adapter not yet implemented")
