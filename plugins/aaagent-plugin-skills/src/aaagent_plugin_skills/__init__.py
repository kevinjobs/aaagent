"""Skills plugin — LLM-authorable, file-backed instructions.

Skills are Markdown files with YAML frontmatter stored under
`data/skills/<name>.md` (configurable via `tools.skills.skills_dir`).
The framework does not inject skill content into the LLM's context
automatically: the LLM must call `load_skill` to bring a skill into
the conversation. This keeps the skill system session-scoped and
self-governing — no cross-session side effects.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from aaagent.core.paths import resolve_project_path
from aaagent.core.plugin import ToolPlugin
from aaagent.core.tool_registry import ToolRegistry

logger = logging.getLogger("aaagent.plugins.skills")

_DEFAULT_SKILLS_DIR = "data/skills"


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if not m:
        return {}, content
    meta_text = m.group(1)
    body = content[m.end():]
    try:
        meta = yaml.safe_load(meta_text) or {}
    except Exception as e:
        logger.debug("Failed to parse skill frontmatter: %s", e)
        meta = {}
    return meta, body


def _write_skill_file(path: Path, name: str, description: str,
                      tags: list[str], instructions: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": name,
        "description": description,
        "tags": tags,
        "created": date.today().isoformat(),
    }
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    content = f"---\n{front}\n---\n\n# {name}\n\n{instructions.strip()}"
    path.write_text(content, encoding="utf-8")


def _read_skill_file(path: Path) -> str:
    """Return full file content as tool response for LLM context."""
    return path.read_text(encoding="utf-8")


def _list_skills_in_dir(skills_dir: Path, tags: list[str] | None = None
                        ) -> list[dict[str, Any]]:
    if not skills_dir.exists():
        return []
    results: list[dict[str, Any]] = []
    for f in sorted(skills_dir.iterdir()):
        if not f.is_file() or not f.name.endswith(".md"):
            continue
        try:
            meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug("Failed to read skill %s: %s", f, e)
            continue
        skill_tags = meta.get("tags", []) or []
        if tags and not any(t in skill_tags for t in tags):
            continue
        results.append({
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "tags": skill_tags,
            "file": f.name,
        })
    return results


def _skills_dir_from_config(config: dict[str, Any]) -> str:
    skills_cfg = (config.get("tools", {}) or {}).get("skills", {}) or {}
    return skills_cfg.get("skills_dir", _DEFAULT_SKILLS_DIR)


def register_skills_tools(
    registry: ToolRegistry,
    skills_dir: str,
) -> None:
    """Register the four skills tools."""

    async def _list_skills(args: dict[str, Any]) -> str:
        tags = args.get("tags")
        if tags is not None and not isinstance(tags, list):
            tags = [tags]
        matches = _list_skills_in_dir(Path(skills_dir), tags=tags)
        if not matches:
            return "没有找到匹配的 skill。"
        lines: list[str] = []
        for m in matches:
            tags_str = ", ".join(m["tags"]) if m["tags"] else "-"
            lines.append(
                f"- **{m['name']}** ({m['file']})\n"
                f"  {m['description']}\n"
                f"  tags: {tags_str}"
            )
        return "\n".join(lines)

    async def _create_skill(args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        description = str(args.get("description", "")).strip()
        instructions = str(args.get("instructions", "")).strip()
        tags = args.get("tags") or []
        if not isinstance(tags, list):
            tags = [tags]
        if not name:
            return "错误：缺少 skill 名称。"
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "unnamed"
        path = Path(skills_dir) / f"{safe_name}.md"
        _write_skill_file(path, safe_name, description, tags, instructions)
        return (
            f"Skill 已保存：`{safe_name}` → `{path}`\n\n"
            f"描述：{description}\n"
            f"标签：{', '.join(tags) if tags else '-'}"
        )

    async def _load_skill(args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            return "错误：缺少 skill 名称。"
        path = Path(skills_dir) / f"{name}.md"
        if not path.exists():
            alt = sorted(f.name[:-3] for f in Path(skills_dir).glob("*.md"))
            extra = ""
            if alt:
                extra = f"\n可用的 skill：{', '.join(alt[:10])}"
            return f"未找到 skill `{name}`。{extra}"
        content = _read_skill_file(path)
        return (
            f"## Skill: {name}\n\n"
            f"来源：`{path}`\n\n"
            f"{content}"
        )

    async def _delete_skill(args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            return "错误：缺少 skill 名称。"
        path = Path(skills_dir) / f"{name}.md"
        if not path.exists():
            return f"未找到 skill `{name}`。"
        path.unlink()
        return f"Skill `{name}` 已删除。"

    registry.register(
        name="list_skills",
        description=(
            "列出已创建的 skill。可选 tags 参数按标签过滤。"
            "适用场景：当用户提到某个主题或工作流时，先检查是否有相关 skill 可用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "按标签过滤，如 ['docker', 'debug']。",
                },
            },
        },
        handler=_list_skills,
    )
    registry.register(
        name="create_skill",
        description=(
            "根据当前对话内容创建一个新 skill。"
            "当发现用户反复请求同类操作、或你正在做一套可复用的工作流时，"
            "主动问用户：'这个流程可以保存为 skill 方便下次复用，要生成吗？'，"
            "确认后再调用 create_skill。不要在没有用户确认时悄悄生成。"
            "name 是 skill 的唯一标识（小写英文+数字），description 一句话说明用途，"
            "instructions 是完整的 LLM 指令正文，tags 用于分类检索。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称（唯一标识，小写英文+数字+下划线）。",
                },
                "description": {
                    "type": "string",
                    "description": "一句话描述 skill 用途。",
                },
                "instructions": {
                    "type": "string",
                    "description": "完整的 LLM 指令正文，告诉 LLM 看到这个 skill 后该怎么做。",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签列表，便于检索。",
                },
            },
            "required": ["name", "description", "instructions"],
        },
        handler=_create_skill,
    )
    registry.register(
        name="load_skill",
        description=(
            "加载一个已存 skill 的完整内容作为工具结果返回。"
            "适用场景：当 list_skills 返回结果、或用户提到某个主题、"
            "或你正在做与某主题相关的任务时，调用 load_skill 把 skill 内容带入当前对话。"
            "Skill 内容会作为 tool response 出现在对话历史中，供你后续引用。"
            "同一 skill 在一个 session 内通常只需加载一次。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称。",
                },
            },
            "required": ["name"],
        },
        handler=_load_skill,
    )
    registry.register(
        name="delete_skill",
        description="删除一个 skill 文件。仅当用户明确要求删除时调用。",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称。",
                },
            },
            "required": ["name"],
        },
        handler=_delete_skill,
    )


class SkillsPlugin(ToolPlugin):
    name = "skills"

    def register(self, registry: ToolRegistry, config: dict[str, Any]) -> None:
        skills_cfg = (config.get("tools", {}) or {}).get("skills", {}) or {}
        if not skills_cfg.get("enabled", True):
            return
        skills_dir = _skills_dir_from_config(config)
        register_skills_tools(registry, skills_dir=skills_dir)