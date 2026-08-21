from __future__ import annotations

import pytest

from aaagent.core.tool_registry import ToolRegistry
from aaagent_plugin_memorytools import MemoryToolsPlugin


class _FakeStore:
    async def remember(self, content, tags=None):
        return content

    async def recall(self, query, top_k=10, tags=None):
        return f"recall({query!r}, top_k={top_k}, tags={tags!r})"


@pytest.mark.asyncio
async def test_recall_forwards_top_k_and_tags():
    store = _FakeStore()
    plugin = MemoryToolsPlugin()
    plugin.set_memory(store)
    reg = ToolRegistry()
    plugin.register(reg, {"memory": {"enabled": True}})
    recall_reg = reg.get("recall")
    assert recall_reg is not None

    result = await reg.execute("recall", '{"query": "foo"}')
    assert "top_k=10" in result
    assert "tags=None" in result

    result2 = await reg.execute(
        "recall",
        '{"query": "bar", "top_k": 3, "tags": ["project", "dev"]}',
    )
    assert "top_k=3" in result2
    assert "project" in result2
    assert "dev" in result2