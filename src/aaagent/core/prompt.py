from __future__ import annotations

from dataclasses import dataclass, field

from aaagent.core.message import Message
from aaagent.core.session import Session


@dataclass
class PromptBuilder:
    """Builds the LLM message list from session + profile + tool definitions.

    Centralizes context assembly so future context fragments (RAG, structured
    preferences, etc.) only need to be added in one place.
    """

    system_prompt: str = ""
    profile_prompt: str = (
        "用户画像仅供参考，不一定是当前用户的最新情况，请通过 recall 工具获取最新信息。"
    )

    def build(
        self,
        session: Session,
        profile: str = "",
        extra_system: list[dict] | None = None,
    ) -> list[dict]:
        msgs: list[dict] = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        if profile:
            msgs.append({
                "role": "system",
                "content": f"## 用户画像（知道的信息）\n\n{profile}\n\n{self.profile_prompt}",
            })
        if session.summary:
            msgs.append({
                "role": "system",
                "content": f"对话历史摘要：{session.summary}",
            })
        if extra_system:
            msgs.extend(extra_system)
        msgs.extend(m.to_llm_dict() for m in session.messages)
        return msgs