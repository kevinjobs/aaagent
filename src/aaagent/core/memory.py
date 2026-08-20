from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aaagent.providers.base import LLMProvider

logger = logging.getLogger("aaagent.memory")

_DATE_FMT = "%Y-%m-%d"
_TIMESTAMP_FMT = "%Y-%m-%d %H:%M"


class MemoryStore:
    def __init__(self, data_dir: str = "data/memories") -> None:
        self._data_dir = Path(data_dir)
        self._facts_dir = self._data_dir / "facts"
        self._profile_path = self._data_dir / "profile.md"
        self._archive_path = self._data_dir / "archive.md"

        self._facts_dir.mkdir(parents=True, exist_ok=True)

        if not self._profile_path.exists():
            self._profile_path.write_text("# 用户画像\n\n", encoding="utf-8")
        if not self._archive_path.exists():
            self._archive_path.write_text("# Session 归档\n\n", encoding="utf-8")

    def _today_facts_path(self) -> Path:
        date_str = time.strftime(_DATE_FMT)
        return self._facts_dir / f"{date_str}.md"

    def _ensure_today_facts(self) -> None:
        p = self._today_facts_path()
        if not p.exists():
            date_str = time.strftime(_DATE_FMT)
            p.write_text(f"# 事实记录 - {date_str}\n\n", encoding="utf-8")

    async def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        session_id: str = "",
    ) -> str:
        tags_str = f"[{', '.join(tags)}]" if tags else ""
        ts = time.strftime(_TIMESTAMP_FMT)

        if tags and "user" in tags:
            profile_entry = f"- {ts} {content}\n"
            with open(self._profile_path, "a", encoding="utf-8") as f:
                f.write(profile_entry)

        self._ensure_today_facts()
        fact_entry = f"- {ts} {tags_str} {content}"
        if session_id:
            fact_entry += f" (session: {session_id})"
        fact_entry += "\n"

        with open(self._today_facts_path(), "a", encoding="utf-8") as f:
            f.write(fact_entry)

        logger.info("Memorized: %s", content[:80])
        return content

    async def recall(self, query: str, top_k: int = 10) -> str:
        query_lower = query.lower()
        results: list[tuple[str, str, float]] = []

        for p in sorted(self._facts_dir.iterdir()):
            if p.suffix != ".md":
                continue
            results.extend(self._search_file(p, query_lower))

        results.extend(self._search_file(self._profile_path, query_lower))

        results.sort(key=lambda x: x[2], reverse=True)

        if not results:
            return "没有找到相关记忆。"

        lines: list[str] = []
        for source, line, _score in results[:top_k]:
            lines.append(f"- [{source}] {line}")

        return "\n".join(lines)

    async def recall_profile(self) -> str:
        if not self._profile_path.exists():
            return ""
        content = self._profile_path.read_text(encoding="utf-8").strip()
        if content == "# 用户画像" or not content:
            return ""
        return content

    def _search_file(self, path: Path, query: str) -> list[tuple[str, str, float]]:
        if not path.exists():
            return []
        results: list[tuple[str, str, float]] = []
        try:
            text = path.read_text(encoding="utf-8")
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
        words = query.split()
        if not words:
            return 0.0
        matches = sum(1 for w in words if w in text)
        return matches / len(words)

    async def archive_session(
        self, session_id: str, summary: str, start_time: float, end_time: float
    ) -> None:
        start_str = time.strftime(_TIMESTAMP_FMT, time.localtime(start_time))
        end_str = time.strftime(_TIMESTAMP_FMT, time.localtime(end_time))
        entry = (
            f"## Session: {session_id}\n"
            f"- 时间：{start_str} - {end_str}\n"
            f"- 摘要：{summary}\n\n"
        )
        with open(self._archive_path, "a", encoding="utf-8") as f:
            f.write(entry)

    def _profile_entry_count(self) -> int:
        if not self._profile_path.exists():
            return 0
        text = self._profile_path.read_text(encoding="utf-8")
        return sum(1 for line in text.split("\n") if line.strip().startswith("- "))

    async def maybe_consolidate_profile(
        self, provider: Any, threshold: int = 15
    ) -> None:
        if self._profile_entry_count() < threshold:
            return

        content = self._profile_path.read_text(encoding="utf-8").strip()
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
                self._profile_path.write_text(consolidated + "\n", encoding="utf-8")
                logger.info("Profile consolidated (%d entries -> merged)", self._profile_entry_count())
            else:
                logger.warning("Profile consolidation output invalid, skipping")
        except Exception as e:
            logger.warning("Profile consolidation failed: %s", e)

    async def close(self) -> None:
        pass
