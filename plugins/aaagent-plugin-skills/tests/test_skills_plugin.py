"""Tests for the skills plugin (create/list/load/delete)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from aaagent.core.tool_registry import ToolRegistry
from aaagent_plugin_skills import SkillsPlugin


def _build_registry(skills_dir: str) -> ToolRegistry:
    cfg = {"tools": {"skills": {"skills_dir": skills_dir, "enabled": True}}}
    plugin = SkillsPlugin()
    registry = ToolRegistry()
    plugin.register(registry, cfg)
    return registry


def _run(reg: ToolRegistry, name: str, args: dict[str, Any]) -> str:
    import json

    return asyncio.run(reg.execute(name, json.dumps(args)))


def test_create_and_load(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}, "skills": {}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)

    create_result = _run(
        reg,
        "create_skill",
        {
            "name": "docker-debug",
            "description": "Docker 容器调试",
            "instructions": "运行 docker ps -a 检查状态",
            "tags": ["docker", "debug"],
        },
    )
    assert "已保存" in create_result
    assert "docker-debug" in create_result

    skill_file = tmp_path / "docker-debug.md"
    assert skill_file.exists()
    content = skill_file.read_text(encoding="utf-8")
    assert "docker-debug" in content
    assert "Docker 容器调试" in content
    assert "docker ps -a" in content
    assert "---" in content

    load_result = _run(reg, "load_skill", {"name": "docker-debug"})
    assert "Skill: docker-debug" in load_result
    assert "Docker 容器调试" in load_result


def test_list_skills(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)

    _run(
        reg,
        "create_skill",
        {"name": "skill-a", "description": "A", "instructions": "A", "tags": ["foo"]},
    )
    _run(
        reg,
        "create_skill",
        {"name": "skill-b", "description": "B", "instructions": "B", "tags": ["bar"]},
    )

    all_skills = _run(reg, "list_skills", {})
    assert "skill-a" in all_skills
    assert "skill-b" in all_skills

    filtered = _run(reg, "list_skills", {"tags": ["foo"]})
    assert "skill-a" in filtered
    assert "skill-b" not in filtered


def test_list_skills_empty(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)
    result = _run(reg, "list_skills", {})
    assert "没有找到" in result


def test_load_missing_skill(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)
    result = _run(reg, "load_skill", {"name": "nonexistent"})
    assert "未找到" in result


def test_load_missing_skill_shows_alternatives(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)

    _run(
        reg,
        "create_skill",
        {"name": "available-skill", "description": "X", "instructions": "X"},
    )
    result = _run(reg, "load_skill", {"name": "missing"})
    assert "未找到" in result
    assert "available-skill" in result


def test_delete_skill(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)

    _run(
        reg,
        "create_skill",
        {"name": "tmp-skill", "description": "tmp", "instructions": "tmp"},
    )
    assert (tmp_path / "tmp-skill.md").exists()

    result = _run(reg, "delete_skill", {"name": "tmp-skill"})
    assert "已删除" in result
    assert not (tmp_path / "tmp-skill.md").exists()


def test_delete_missing_skill(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)
    result = _run(reg, "delete_skill", {"name": "nonexistent"})
    assert "未找到" in result


def test_create_skill_missing_name(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)
    result = _run(
        reg,
        "create_skill",
        {"name": "", "description": "bad", "instructions": "bad"},
    )
    assert "错误" in result
    assert not any(tmp_path.glob("*.md"))


def test_create_skill_sanitises_name(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)

    _run(
        reg,
        "create_skill",
        {
            "name": "my  skill!@#",
            "description": "sanitised",
            "instructions": "x",
            "tags": ["test"],
        },
    )
    expected = tmp_path / "my__skill.md"
    assert expected.exists()


def test_load_skill_shows_source_path(tmp_path):
    cfg = {"tools": {"skills": {"skills_dir": str(tmp_path)}}}
    plugin = SkillsPlugin()
    reg = ToolRegistry()
    plugin.register(reg, cfg)

    _run(
        reg,
        "create_skill",
        {"name": "loc-test", "description": "path test", "instructions": "hello"},
    )
    result = _run(reg, "load_skill", {"name": "loc-test"})
    assert str(tmp_path) in result or tmp_path.name in result