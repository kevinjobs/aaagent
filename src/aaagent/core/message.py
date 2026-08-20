from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import uuid
import time


@dataclass
class Message:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = ""
    platform: str = ""
    chat_id: str = ""
    user_id: str = ""
    content: str = ""
    role: str = "user"
    raw: Any = None
    timestamp: float = field(default_factory=time.time)
    tool_call_id: str = ""
    name: str = ""
    tool_calls: list[dict[str, Any]] | None = None

    def to_llm_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role}
        if self.role == "tool":
            d["tool_call_id"] = self.tool_call_id
            d["content"] = self.content
        elif self.tool_calls:
            d["content"] = self.content or None
            d["tool_calls"] = self.tool_calls
        else:
            d["content"] = self.content
        return d
