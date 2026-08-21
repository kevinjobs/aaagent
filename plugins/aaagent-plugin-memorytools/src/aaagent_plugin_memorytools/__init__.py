from __future__ import annotations

from typing import Any

from aaagent.core.plugin import ToolPlugin


def register_memory_tools(registry: Any, memory_store: Any | None = None) -> None:
    """Register remember / recall tools that operate on a MemoryStore."""
    store_ref: list[Any] = [memory_store]

    async def _remember(args: dict[str, Any]) -> str:
        if store_ref[0] is None:
            return "Error: memory store not initialized"
        store = store_ref[0]
        content = args["content"]
        tags = args.get("tags", None)
        result = await store.remember(content=content, tags=tags)
        return f"已记住：{result}"

    async def _recall(args: dict[str, Any]) -> str:
        if store_ref[0] is None:
            return "Error: memory store not initialized"
        store = store_ref[0]
        query = args["query"]
        top_k = int(args.get("top_k", 10) or 10)
        tags: list[str] | None = args.get("tags") or None
        if tags is not None and not isinstance(tags, list):
            tags = [tags]
        result = await store.recall(query=query, top_k=top_k, tags=tags)
        return result

    registry.register(
        name="remember",
        description="记住一条信息。用于长期保存用户偏好、身份信息、重要事实等。当你发现需要记住某件事时，请主动调用。",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记住的内容",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签，如 ['user', 'project', 'fact']。标记 user 的内容会写入用户画像。",
                },
            },
            "required": ["content"],
        },
        handler=_remember,
    )
    registry.register(
        name="recall",
        description="回忆已记住的信息。当你需要了解用户偏好、之前讨论过的事实、或项目历史信息时调用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，越具体越好",
                },
                "top_k": {
                    "type": "integer",
                    "description": "最多返回的记忆条数（默认 10）",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "按标签过滤（可选），如 ['project']",
                },
            },
            "required": ["query"],
        },
        handler=_recall,
    )


class MemoryToolsPlugin(ToolPlugin):
    name = "memory"
    uses_memory_store = True

    def __init__(self) -> None:
        self._memory_store: Any = None

    def set_memory(self, store: Any) -> None:
        self._memory_store = store

    def register(self, registry: Any, config: dict[str, Any]) -> None:
        memory_cfg = config.get("memory", {}) if isinstance(config, dict) else {}
        if not memory_cfg.get("enabled", True):
            return
        register_memory_tools(registry, memory_store=self._memory_store)


__all__ = ["MemoryToolsPlugin"]