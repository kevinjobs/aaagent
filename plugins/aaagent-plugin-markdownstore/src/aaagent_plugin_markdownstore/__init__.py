from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
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
_SESSION_RE = re.compile(r"\s*\(session:\s*([^)]+)\)\s*$")
_TAGS_RE = re.compile(r"^\[([^\]]*)\]\s*")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*")

_BONUS_PER_TAG = 0.15
_BONUS_CAP = 0.3
_RECENCY_FLOOR = 0.1


@dataclass
class FactEntry:
    """A single parsed memory fact line (`- ...` in a facts/profile file)."""

    raw: str
    source: str
    content: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)
    ts: float | None = None
    session: str = ""


def _parse_fact_line(line: str, source: str) -> FactEntry | None:
    """Parse a `- ...` line into a FactEntry (None for headers/blank lines)."""
    raw = line.strip()
    if not raw or not raw.startswith("- "):
        return None

    session = ""
    m = _SESSION_RE.search(raw)
    if m:
        session = m.group(1)
        raw = raw[: m.start()]

    body = raw[2:].strip()

    ts: float | None = None
    m = _TS_RE.match(body)
    if m:
        try:
            ts = time.mktime(time.strptime(m.group(1), _TIMESTAMP_FMT))
            body = body[m.end():].strip()
        except ValueError:
            ts = None

    tags: frozenset[str] = frozenset()
    m = _TAGS_RE.match(body)
    if m:
        tags = frozenset(
            t.strip() for t in m.group(1).split(",") if t.strip()
        )
        body = body[m.end():].strip()

    content = body
    return FactEntry(
        raw=line.strip(),
        source=source,
        content=content,
        tags=tags,
        ts=ts,
        session=session,
    )


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


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

    def __init__(
        self,
        data_dir: str = "data/memories",
        base_path: Path | None = None,
        relevance_weight: float = 0.7,
        recency_weight: float = 0.3,
        recency_decay: float = 0.1,
        tag_bonus: float = _BONUS_PER_TAG,
        tag_bonus_cap: float = _BONUS_CAP,
    ) -> None:
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

        self._relevance_weight = float(relevance_weight)
        self._recency_weight = float(recency_weight)
        self._recency_decay = float(recency_decay)
        self._tag_bonus = float(tag_bonus)
        self._tag_bonus_cap = float(tag_bonus_cap)

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

    async def recall(
        self, query: str, top_k: int = 10, tags: list[str] | None = None
    ) -> str:
        entries = await self._load_entries()

        q_tags: set[str] = set(tags or [])
        if q_tags:
            entries = [e for e in entries if q_tags & e.tags]
            if not entries:
                return "没有找到相关记忆。"

        q_tokens = _tokenize(query)
        if not q_tokens:
            return "没有找到相关记忆。"

        idf = self._compute_idf(entries, q_tokens)
        denom = sum(idf.values()) or 1.0
        now = time.time()

        matches: list[tuple[str, str, float]] = []
        for e in entries:
            e_tokens = set(_tokenize(e.content))
            lex = sum(idf[t] for t in q_tokens if t in e_tokens) / denom
            if lex <= 0:
                continue
            rec = self._recency_score(e.ts, now)
            bonus = min(len(q_tags & e.tags) * self._tag_bonus, self._tag_bonus_cap)
            combined = (
                self._relevance_weight * lex + self._recency_weight * rec + bonus
            )
            matches.append((e.source, e.raw, combined))

        matches.sort(key=lambda x: x[2], reverse=True)

        if not matches:
            return "没有找到相关记忆。"

        lines = [f"- [{source}] {raw}" for source, raw, _score in matches[:top_k]]
        return "\n".join(lines)

    @staticmethod
    def _compute_idf(entries: list[FactEntry], q_tokens: list[str]) -> dict[str, float]:
        n_docs = max(1, len(entries))
        df: Counter[str] = Counter()
        for e in entries:
            for t in set(_tokenize(e.content)):
                df[t] += 1
        return {
            t: math.log(1 + (n_docs - df[t] + 0.5) / (df[t] + 0.5))
            for t in q_tokens
        }

    def _recency_score(self, ts: float | None, now: float) -> float:
        if ts is None:
            return 0.5
        age_hours = max(0.0, (now - ts) / 3600)
        return max(_RECENCY_FLOOR, math.exp(-self._recency_decay * age_hours / 24))

    async def _load_entries(self) -> list[FactEntry]:
        entries: list[FactEntry] = []
        async with self._lock:
            if await aiofiles.os.path.isdir(self._facts_dir):
                for name in await aiofiles.os.listdir(self._facts_dir):
                    if not name.endswith(".md"):
                        continue
                    entries.extend(await self._read_entries(self._facts_dir / name))
            entries.extend(await self._read_entries(self._profile_path))
        return entries

    async def _read_entries(self, path: Path) -> list[FactEntry]:
        if not await aiofiles.os.path.exists(path):
            return []
        try:
            async with aiofiles.open(path, encoding="utf-8") as f:
                text = await f.read()
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to read %s: %s", path, e)
            return []
        source = "profile" if path.name == "profile.md" else path.stem
        result: list[FactEntry] = []
        for line in text.split("\n"):
            entry = _parse_fact_line(line, source)
            if entry is not None:
                result.append(entry)
        return result

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
        recall_cfg = config.get("recall", {}) or {}
        return MarkdownMemoryStore(
            data_dir=data_dir,
            base_path=base_path,
            relevance_weight=float(recall_cfg.get("relevance_weight", 0.7)),
            recency_weight=float(recall_cfg.get("recency_weight", 0.3)),
            recency_decay=float(recall_cfg.get("recency_decay", 0.1)),
        )


__all__ = ["MarkdownMemoryStore", "MarkdownMemoryStoreFactory"]