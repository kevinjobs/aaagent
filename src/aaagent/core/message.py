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

    def to_llm_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}
