from __future__ import annotations

import asyncio

import pytest

from aaagent.core.memory import MemoryStore


@pytest.mark.asyncio
async def test_remember_concurrent(tmp_memory_dir):
    store = MemoryStore(data_dir="data", base_path=tmp_memory_dir)

    await asyncio.gather(
        *(store.remember(f"fact-{i}") for i in range(50))
    )

    facts_file = store._today_facts_path()
    text = facts_file.read_text(encoding="utf-8")
    lines = [ln for ln in text.split("\n") if ln.startswith("- ")]
    assert len(lines) == 50


@pytest.mark.asyncio
async def test_recall_profile_returns_empty_when_no_entries(tmp_memory_dir):
    store = MemoryStore(data_dir="data", base_path=tmp_memory_dir)
    assert await store.recall_profile() == ""


@pytest.mark.asyncio
async def test_remember_with_user_tag_writes_profile(tmp_memory_dir):
    store = MemoryStore(data_dir="data", base_path=tmp_memory_dir)
    await store.remember("likes python", tags=["user"])

    profile = await store.recall_profile()
    assert "likes python" in profile


@pytest.mark.asyncio
async def test_recall_finds_user_tagged_entry(tmp_memory_dir):
    store = MemoryStore(data_dir="data", base_path=tmp_memory_dir)
    await store.remember("likes python", tags=["user"])
    result = await store.recall("python")
    assert "python" in result


@pytest.mark.asyncio
async def test_match_score_filters_short_tokens():
    assert MemoryStore._match_score("some text here", "a") == 0.0
    assert MemoryStore._match_score("python programming", "py") == 0.0
    assert MemoryStore._match_score("python programming", "python") == 1.0


@pytest.mark.asyncio
async def test_match_score_handles_chinese():
    score = MemoryStore._match_score("喜欢 python 编程", "python 编程")
    assert score > 0


@pytest.mark.asyncio
async def test_maybe_consolidate_skips_when_below_threshold(tmp_memory_dir, fake_profile_provider):
    store = MemoryStore(data_dir="data", base_path=tmp_memory_dir)
    for i in range(5):
        await store.remember(f"fact {i}", tags=["user"])
    await store.maybe_consolidate_profile(fake_profile_provider, threshold=15)
    assert fake_profile_provider.calls == 0


@pytest.mark.asyncio
async def test_maybe_consolidate_runs_above_threshold(tmp_memory_dir, fake_profile_provider):
    store = MemoryStore(data_dir="data", base_path=tmp_memory_dir)
    for i in range(20):
        await store.remember(f"fact {i}", tags=["user"])
    await store.maybe_consolidate_profile(fake_profile_provider, threshold=15)
    assert fake_profile_provider.calls == 1
    text = (store._profile_path).read_text(encoding="utf-8")
    assert text.startswith("# 用户画像")