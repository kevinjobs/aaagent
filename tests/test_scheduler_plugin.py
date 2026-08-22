"""Tests for the scheduler plugin (create / list / remove / update / fire)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from aaagent.core.bus import EventBus
from aaagent.core.logctx import set_context
from aaagent.core.message import Message
from aaagent.core.tool_registry import ToolRegistry
from aaagent_plugin_scheduler import (
    SchedulerToolsPlugin,
    _SchedulerStore,
    _compute_next_cron,
    _now,
)


def _reg(store: _SchedulerStore, app: Any = None) -> ToolRegistry:
    cfg = {
        "tools": {"scheduler": {"storage_path": str(store._path), "enabled": True}}
    }
    plugin = SchedulerToolsPlugin()
    plugin._store = store
    if app is not None:
        plugin._app = app
    reg = ToolRegistry()
    plugin.register(reg, cfg)
    return reg


def _run(reg: ToolRegistry, name: str, args: dict[str, Any],
         user_id: str = "u-test") -> str:
    tok = set_context(user_id=user_id)
    try:
        return asyncio.run(reg.execute(name, json.dumps(args)))
    finally:
        # Cleanup context vars
        from aaagent.core.logctx import _user_id
        _user_id.set("")


class _FakeApp:
    """Minimal stand-in for Application — exposes `_bus`."""

    def __init__(self, project_root: Path | None = None):
        self._bus = EventBus()
        self._project_root = project_root or Path.cwd()


# ─────────────────────────────────────────────────────────────────────────────
# store-level tests
# ─────────────────────────────────────────────────────────────────────────────


def test_store_roundtrip(tmp_path: Path):
    path = tmp_path / "schedules.json"
    store = _SchedulerStore(path)
    assert store.load_all() == []
    sample = [{"id": "abc", "type": "recurring", "cron": "0 9 * * *"}]
    store.save_all(sample)
    assert store.load_all() == sample


def test_store_atomic_write_leaves_no_tmp(tmp_path: Path):
    path = tmp_path / "schedules.json"
    store = _SchedulerStore(path)
    store.save_all([{"id": "x"}])
    leftover = list(path.parent.glob("schedules.json.*.tmp"))
    assert not leftover


# ─────────────────────────────────────────────────────────────────────────────
# tool-handler tests
# ─────────────────────────────────────────────────────────────────────────────


def test_create_recurring(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)

    out = _run(
        reg,
        "schedule_create",
        {
            "prompt": "提醒我看天气",
            "platform": "feishu",
            "chat_id": "oc_1",
            "user_id": "u_1",
            "cron": "0 9 * * *",
        },
        user_id="u_1",
    )
    assert "已创建" in out
    assert "首次触发" in out

    saved = store.load_all()
    assert len(saved) == 1
    s = saved[0]
    assert s["type"] == "recurring"
    assert s["cron"] == "0 9 * * *"
    assert s["creator_user_id"] == "u_1"
    assert s["enabled"] is True
    assert s["next_fire_at"]  # computed


def test_create_once_at_iso(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    fire = (_now() + timedelta(hours=2)).isoformat()

    out = _run(
        reg,
        "schedule_create",
        {
            "prompt": "开会",
            "platform": "cli",
            "chat_id": "cli-default",
            "user_id": "u_local",
            "at": fire,
        },
        user_id="u_local",
    )
    assert "已创建" in out

    s = store.load_all()[0]
    assert s["type"] == "once"
    assert s["at"] == fire
    assert s["next_fire_at"] == fire


def test_create_auto_detects_type(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    _run(
        reg,
        "schedule_create",
        {
            "prompt": "x",
            "platform": "feishu",
            "chat_id": "oc_1",
            "user_id": "u_1",
            "cron": "*/5 * * * *",
        },
        user_id="u_1",
    )
    s = store.load_all()[0]
    assert s["type"] == "recurring"


def test_create_missing_required_field(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    out = _run(
        reg,
        "schedule_create",
        {"platform": "feishu", "chat_id": "oc_1", "user_id": "u_1"},
        user_id="u_1",
    )
    assert "错误" in out
    assert "prompt" in out
    assert not store.load_all()


def test_create_no_cron_no_at(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    out = _run(
        reg,
        "schedule_create",
        {
            "prompt": "x",
            "platform": "feishu",
            "chat_id": "oc_1",
            "user_id": "u_1",
        },
        user_id="u_1",
    )
    assert "错误" in out
    assert "cron" in out or "at" in out


def test_create_invalid_cron(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    out = _run(
        reg,
        "schedule_create",
        {
            "prompt": "x",
            "platform": "feishu",
            "chat_id": "oc_1",
            "user_id": "u_1",
            "cron": "not-a-cron",
        },
        user_id="u_1",
    )
    assert "无效 cron" in out
    assert not store.load_all()


def test_create_invalid_iso_at(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    out = _run(
        reg,
        "schedule_create",
        {
            "prompt": "x",
            "platform": "feishu",
            "chat_id": "oc_1",
            "user_id": "u_1",
            "at": "not-a-date",
        },
        user_id="u_1",
    )
    assert "ISO datetime" in out
    assert not store.load_all()


def test_session_id_defaults_to_platform_chat(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    _run(
        reg,
        "schedule_create",
        {
            "prompt": "x",
            "platform": "feishu",
            "chat_id": "oc_xyz",
            "user_id": "u_1",
            "cron": "0 9 * * *",
        },
        user_id="u_1",
    )
    s = store.load_all()[0]
    assert s["session_id"] == "feishu-oc_xyz"


def test_list_filters_by_user(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    # u1 creates two schedules
    for cron in ("0 9 * * *", "0 17 * * *"):
        _run(
            reg,
            "schedule_create",
            {
                "prompt": f"x {cron}",
                "platform": "feishu",
                "chat_id": "oc_1",
                "user_id": "u_1",
                "cron": cron,
            },
            user_id="u_1",
        )
    # u2 creates one
    _run(
        reg,
        "schedule_create",
        {
            "prompt": "y",
            "platform": "feishu",
            "chat_id": "oc_2",
            "user_id": "u_2",
            "cron": "0 12 * * *",
        },
        user_id="u_2",
    )
    out_u1 = _run(reg, "schedule_list", {}, user_id="u_1")
    out_u2 = _run(reg, "schedule_list", {}, user_id="u_2")
    assert out_u1.count("- **") == 2
    assert out_u2.count("- **") == 1


def test_remove_owner_only(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    _run(
        reg,
        "schedule_create",
        {
            "prompt": "x",
            "platform": "feishu",
            "chat_id": "oc_1",
            "user_id": "u_1",
            "cron": "0 9 * * *",
        },
        user_id="u_1",
    )
    sid = store.load_all()[0]["id"]
    # u2 tries to remove
    out_other = _run(reg, "schedule_remove", {"schedule_id": sid}, user_id="u_2")
    assert "无权" in out_other
    assert len(store.load_all()) == 1
    # u1 removes
    out_owner = _run(reg, "schedule_remove", {"schedule_id": sid}, user_id="u_1")
    assert "已删除" in out_owner
    assert not store.load_all()


def test_remove_missing(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    out = _run(reg, "schedule_remove", {"schedule_id": "nope"}, user_id="u_1")
    assert "未找到" in out


def test_update_disable_then_enable(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    reg = _reg(store)
    _run(
        reg,
        "schedule_create",
        {
            "prompt": "x",
            "platform": "feishu",
            "chat_id": "oc_1",
            "user_id": "u_1",
            "cron": "0 9 * * *",
        },
        user_id="u_1",
    )
    sid = store.load_all()[0]["id"]

    out = _run(reg, "schedule_update", {"schedule_id": sid, "enabled": False}, user_id="u_1")
    assert "停用" in out
    assert store.load_all()[0]["enabled"] is False

    out2 = _run(reg, "schedule_update", {"schedule_id": sid, "enabled": True}, user_id="u_1")
    assert "启用" in out2
    assert store.load_all()[0]["enabled"] is True


# ─────────────────────────────────────────────────────────────────────────────
# tick-loop / firing tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_fires_due_recurring(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    app = _FakeApp()
    fired: list[Message] = []
    app._bus.on("message_received", fired.append)

    plugin = SchedulerToolsPlugin()
    plugin._store = store
    plugin._app = app
    plugin._tick_seconds = 1

    # Hand-write a schedule whose next_fire is in the past
    past = (_now() - timedelta(minutes=1)).isoformat()
    store.save_all([{
        "id": "abc",
        "type": "recurring",
        "creator_user_id": "u_1",
        "platform": "cli",
        "chat_id": "cli-default",
        "user_id": "u_1",
        "session_id": "cli-cli-default",
        "prompt": "fire now",
        "cron": "*/5 * * * *",
        "enabled": True,
        "next_fire_at": past,
        "last_fired_at": None,
        "created_at": _now().isoformat(),
    }])

    await plugin._check_and_fire()

    assert len(fired) == 1
    assert fired[0].content == "fire now"
    assert fired[0].platform == "cli"
    assert fired[0].raw["trigger"] == "scheduler"

    s = store.load_all()[0]
    assert s["last_fired_at"] is not None
    assert s["next_fire_at"] != past  # recomputed


@pytest.mark.asyncio
async def test_tick_once_disables_after_fire(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    app = _FakeApp()
    fired: list[Message] = []
    app._bus.on("message_received", fired.append)

    plugin = SchedulerToolsPlugin()
    plugin._store = store
    plugin._app = app

    past = (_now() - timedelta(seconds=10)).isoformat()
    store.save_all([{
        "id": "once1",
        "type": "once",
        "creator_user_id": "u_1",
        "platform": "cli",
        "chat_id": "cli-default",
        "user_id": "u_1",
        "session_id": "cli-cli-default",
        "prompt": "one-shot",
        "at": past,
        "enabled": True,
        "next_fire_at": past,
        "last_fired_at": None,
        "created_at": _now().isoformat(),
    }])

    await plugin._check_and_fire()
    assert len(fired) == 1

    s = store.load_all()[0]
    assert s["enabled"] is False
    assert "next_fire_at" not in s


@pytest.mark.asyncio
async def test_tick_skips_future_schedule(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    app = _FakeApp()
    fired: list[Message] = []
    app._bus.on("message_received", fired.append)

    plugin = SchedulerToolsPlugin()
    plugin._store = store
    plugin._app = app

    future = (_now() + timedelta(hours=1)).isoformat()
    store.save_all([{
        "id": "later",
        "type": "recurring",
        "creator_user_id": "u_1",
        "platform": "cli",
        "chat_id": "cli-default",
        "user_id": "u_1",
        "session_id": "cli-cli-default",
        "prompt": "later",
        "cron": "0 9 * * *",
        "enabled": True,
        "next_fire_at": future,
        "last_fired_at": None,
        "created_at": _now().isoformat(),
    }])

    await plugin._check_and_fire()
    assert fired == []
    s = store.load_all()[0]
    assert s["next_fire_at"] == future  # unchanged


@pytest.mark.asyncio
async def test_tick_skips_disabled(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    app = _FakeApp()
    fired: list[Message] = []
    app._bus.on("message_received", fired.append)

    plugin = SchedulerToolsPlugin()
    plugin._store = store
    plugin._app = app

    past = (_now() - timedelta(seconds=1)).isoformat()
    store.save_all([{
        "id": "off",
        "type": "recurring",
        "creator_user_id": "u_1",
        "platform": "cli",
        "chat_id": "cli-default",
        "user_id": "u_1",
        "session_id": "cli-cli-default",
        "prompt": "x",
        "cron": "*/5 * * * *",
        "enabled": False,
        "next_fire_at": past,
        "last_fired_at": None,
        "created_at": _now().isoformat(),
    }])

    await plugin._check_and_fire()
    assert fired == []


# ─────────────────────────────────────────────────────────────────────────────
# croniter sanity
# ─────────────────────────────────────────────────────────────────────────────


def test_cron_5field():
    base = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)
    nxt = _compute_next_cron("0 9 * * *", base)
    assert nxt.hour == 9
    assert nxt.day == 22


def test_cron_step_values():
    base = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)
    nxt = _compute_next_cron("*/15 * * * *", base)
    assert nxt.minute in (15, 30, 45, 0)
    assert nxt > base


# ─────────────────────────────────────────────────────────────────────────────
# full lifecycle (register / establish / close)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plugin_establish_spawns_tick_task(tmp_path: Path):
    store = _SchedulerStore(tmp_path / "sched.json")
    app = _FakeApp()

    cfg = {
        "tools": {
            "scheduler": {
                "storage_path": str(store._path),
                "enabled": True,
                "tick_seconds": 1,
            }
        }
    }
    plugin = SchedulerToolsPlugin()
    plugin.set_application(app)
    reg = ToolRegistry()
    plugin.register(reg, cfg)

    await plugin.establish(reg, cfg)
    assert plugin._task is not None
    assert not plugin._task.done()

    await plugin.close()
    assert plugin._task is None or plugin._task.done()