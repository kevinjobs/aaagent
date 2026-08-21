from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
import aiofiles.os

from aaagent.core.memory import MemoryStore
from aaagent.core.plugin import MemoryStoreFactory

if TYPE_CHECKING:
    from aaagent.core.types import LLMProvider

logger = logging.getLogger("aaagent.markdownstore")

_DATE_FMT = "%Y-%m-%d"
_TIMESTAMP_FMT = "%Y-%m-%d %H:%M"
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z]{2,}|\d{2,}")


class MarkdownMemoryStore(MemoryStore):
    """Markdown-file backed memory store.

    Maintains three Markdown files under `data_dir`:
    - profile.md (consolidated when entries >= 15)
    - facts/YYYY-MM-DD.md (chronological fact log)
    - archive.md (session archives)

    Concurrent writes are serialized with an asyncio.Lock. The
    consolidate flow uses snapshot-release-LLM-reacquire-write so the
    lock is not held during the network call to the LLM.
    """

    def __init__(self, data_dir: str = "data/memories", base_path: Path | None = None) -> None:
        if base_path is not None:
            base = Path(base_path).resolve()
        else:
            base = Path.cwd()
        raw = Path(data_dir)
        if raw.is_absolute():
            self._data_dir = raw
        else:
            self._data_dir = base / raw
        self._facts_dir = self._data_dir / "facts"
        self._profile_path = self._data_dir / "profile.md"
        self._archive_path = self._data_dir / "archive.md"
        self._lock = __import__("asyncio").Lock()

        self._facts_dir.mkdir(parents=True, exist_ok=True)
        if not self._profile_path.exists():
            self._profile_path.write_text("# 用户画像\n\n", encoding="utf-8")
        if not self._archive_path.exists():
            self._archive_path.write_text("# Session 归档\n\n", encoding="utf-8")

    def _today_facts_path(self) -> Path:
        date_str = time.strftime(_DATE_FMT)
        return self._facts_dir / f"{date_str}.md"

    async def _ensure_today_facts(self) -> None:
        p = self._today_facts_path()
        if not await aiofiles.os.path.exists(p):
            date_str = time.strftime(_DATE_FMT)
            async with aiofiles.open(p, "w", encoding="utf-8") as f:
                await f.write(f"# 事实记录 - {date_str}\n\n")

    async def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        session_id: str = "",
    ) -> str:
        import asyncio

        tags_str = f"[{', '.join(tags)}]" if tags else ""
        ts = time.strftime(_TIMESTAMP_FMT)

        async with self._lock:
            if tags and "user" in tags:
                async with aiofiles.open(self._profile_path, "a", encoding="utf-8") as f:
                    await f.write(f"- {ts} {content}\n")

            await self._ensure_today_facts()
            fact_entry = f"- {ts} {tags_str} {content}"
            if session_id:
                fact_entry += f" (session: {session_id})"
            fact_entry += "\n"
            async with aiofiles.open(self._today_facts_path(), "a", encoding="utf-8") as f:
                await f.write(fact_entry)

        logger.info("Memorized: %s", content[:80])
        return content

    async def recall(self, query: str, top_k: int = 10) -> str:
        query_lower = query.lower()
        results: list[tuple[str, str, float]] = []

        async with self._lock:
            fact_files = [p for p in await aiofiles.os.listdir(self._facts_dir) if p.endswith(".md")]
            for name in fact_files:
                p = self._facts_dir / name
                results.extend(await self._search_file(p, query_lower))
            results.extend(await self._search_file(self._profile_path, query_lower))

        results.sort(key=lambda x: x[2], reverse=True)

        if not results:
            return "没有找到相关记忆。"

        lines: list[str] = []
        for source, line, _score in results[:top_k]:
            lines.append(f"- [{source}] {line}")

        return "\n".join(lines)

    async def recall_profile(self) -> str:
        import asyncio

        async with self._lock:
            if not self._profile_path.exists():
                return ""
            async with aiofiles.open(self._profile_path, encoding="utf-8") as f:
                content = (await f.read()).strip()
        if content == "# 用户画像" or not content:
            return ""
        return content

    async def _search_file(self, path: Path, query: str) -> list[tuple[str, str, float]]:
        if not await aiofiles.os.path.exists(path):
            return []
        results: list[tuple[str, str, float]] = []
        try:
            async with aiofiles.open(path, encoding="utf-8") as f:
                text = await f.read()
            for line in text.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if not line.startswith("- "):
                    continue
                score = self._match_score(line.lower(), query)
                if score > 0:
                    results.append((path.stem, line, score))
        except Exception as e:
            logger.warning("Failed to search %s: %s", path, e)
        return results

    @staticmethod
    def _match_score(text: str, query: str) -> float:
        if not query:
            return 0.0
        q_tokens = _TOKEN_RE.findall(query.lower())
        if not q_tokens:
            return 0.0
        t_tokens = set(_TOKEN_RE.findall(text.lower()))
        hits = sum(1 for qt in q_tokens if qt in t_tokens)
        return hits / len(q_tokens)

    async def archive_session(
        self, session_id: str, summary: str, start_time: float, end_time: float
    ) -> None:
        import asyncio

        start_str = time.strftime(_TIMESTAMP_FMT, time.localtime(start_time))
        end_str = time.strftime(_TIMESTAMP_FMT, time.localtime(end_time))
        entry = (
            f"## Session: {session_id}\n"
            f"- 时间：{start_str} - {end_str}\n"
            f"- 摘要：{summary}\n\n"
        )
        async with self._lock:
            async with aiofiles.open(self._archive_path, "a", encoding="utf-8") as f:
                await f.write(entry)

    async def _profile_entry_count(self) -> int:
        import asyncio

        async with self._lock:
            if not self._profile_path.exists():
                return 0
            async with aiofiles.open(self._profile_path, encoding="utf-8") as f:
                text = await f.read()
        return sum(1 for line in text.split("\n") if line.strip().startswith("- "))

    async def maybe_consolidate_profile(
        self, provider: Any, threshold: int = 15
    ) -> None:
        import asyncio

        if await self._profile_entry_count() < threshold:
            return

        async with self._lock:
            if not self._profile_path.exists():
                return
            async with aiofiles.open(self._profile_path, encoding="utf-8") as f:
                content = (await f.read()).strip()

        prompt = (
            "请将以下用户画像条目合并为一段简洁、无重复、无矛盾的用户画像。\n"
            "保留最新信息，去除过时条目。用 Markdown 列表格式输出，"
            "以 '# 用户画像' 开头。\n\n"
            f"{content}"
        )

        try:
            result = await provider.chat([{"role": "user", "content": prompt}])
            consolidated = result.content.strip()
            if consolidated and "# 用户画像" in consolidated:
                async with self._lock:
                    async with aiofiles.open(self._profile_path, "w", encoding="utf-8") as f:
                        await f.write(consolidated + "\n")
                logger.info(
                    "Profile consolidated (%d entries -> merged)",
                    await self._profile_entry_count(),
                )
            else:
                logger.warning("Profile consolidation output invalid, skipping")
        except Exception as e:
            logger.warning("Profile consolidation failed: %s", e)

    async def close(self) -> None:
        pass


class MarkdownMemoryStoreFactory(MemoryStoreFactory):
    """Plugin factory for the Markdown-backed memory store."""

    name = "markdown"

    def create(self, config: dict) -> MemoryStore:
        data_dir = config.get("data_dir", "data/memories")
        base_path_raw = config.get("base_path")
        base_path = Path(base_path_raw) if base_path_raw else None
        return MarkdownMemoryStore(data_dir=data_dir, base_path=base_path)


__all__ = ["MarkdownMemoryStore", "MarkdownMemoryStoreFactory"]