from __future__ import annotations

import asyncio
import time

import pytest

from aaagent_plugin_markdownstore import MarkdownMemoryStore


def _store(tmp_memory_dir):
    return MarkdownMemoryStore(data_dir="data", base_path=tmp_memory_dir)


@pytest.mark.asyncio
async def test_remember_concurrent(tmp_memory_dir):
    store = _store(tmp_memory_dir)

    await asyncio.gather(
        *(store.remember(f"fact-{i}") for i in range(50))
    )

    facts_file = store._today_facts_path()
    text = facts_file.read_text(encoding="utf-8")
    lines = [ln for ln in text.split("\n") if ln.startswith("- ")]
    assert len(lines) == 50


@pytest.mark.asyncio
async def test_recall_profile_returns_empty_when_no_entries(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    assert await store.recall_profile() == ""


@pytest.mark.asyncio
async def test_remember_with_user_tag_writes_profile(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    await store.remember("likes python", tags=["user"])

    profile = await store.recall_profile()
    assert "likes python" in profile


@pytest.mark.asyncio
async def test_recall_finds_user_tagged_entry(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    await store.remember("likes python", tags=["user"])
    result = await store.recall("python")
    assert "python" in result


@pytest.mark.asyncio
async def test_recall_rejects_short_queries(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    await store.remember("python programming is fun")
    assert "没有找到相关记忆" in await store.recall("a")
    assert "没有找到相关记忆" in await store.recall("")


@pytest.mark.asyncio
async def test_recall_chinese_query(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    await store.remember("喜欢 python 编程")
    r = await store.recall("编程")
    assert "编程" in r


@pytest.mark.asyncio
async def test_recall_top_k_limits_results(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    for i in range(5):
        await store.remember(f"shared keyword item {i}")
    result = await store.recall("keyword", top_k=2)
    assert len(result.split("\n")) == 2


@pytest.mark.asyncio
async def test_recall_tag_filter_hits_only_matching(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    await store.remember("project plan Q3", tags=["project"])
    await store.remember("likes tea and coffee", tags=["user"])
    r = await store.recall("plan", tags=["project"])
    assert "project plan Q3" in r
    assert "likes tea" not in r
    r2 = await store.recall("likes", tags=["user"])
    assert "likes tea" in r2
    assert "project plan" not in r2


@pytest.mark.asyncio
async def test_recall_tag_filter_returns_nothing_when_no_overlap(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    await store.remember("project plan Q3", tags=["project"])
    r = await store.recall("plan", tags=["user"])
    assert "没有找到相关记忆" in r


@pytest.mark.asyncio
async def test_recall_prefers_recent_entries(tmp_memory_dir):
    store = _store(tmp_memory_dir)
    now = time.time()
    older = time.strftime("%Y-%m-%d %H:%M", time.localtime(now - 7200))
    newer = time.strftime("%Y-%m-%d %H:%M", time.localtime(now - 3600))
    d = store._facts_dir
    (d / "older.md").write_text(f"# old\n\n- {older} alpha beta gamma\n", encoding="utf-8")
    (d / "newer.md").write_text(f"# new\n\n- {newer} alpha beta gamma\n", encoding="utf-8")
    result = await store.recall("alpha")
    first_line = result.split("\n")[0]
    assert "- [newer]" in first_line


@pytest.mark.asyncio
async def test_maybe_consolidate_skips_when_below_threshold(tmp_memory_dir, fake_profile_provider):
    store = _store(tmp_memory_dir)
    for i in range(5):
        await store.remember(f"fact {i}", tags=["user"])
    await store.maybe_consolidate_profile(fake_profile_provider, threshold=15)
    assert fake_profile_provider.calls == 0


@pytest.mark.asyncio
async def test_maybe_consolidate_runs_above_threshold(tmp_memory_dir, fake_profile_provider):
    store = _store(tmp_memory_dir)
    for i in range(20):
        await store.remember(f"fact {i}", tags=["user"])
    await store.maybe_consolidate_profile(fake_profile_provider, threshold=15)
    assert fake_profile_provider.calls == 1
    text = (store._profile_path).read_text(encoding="utf-8")
    assert text.startswith("# 用户画像")