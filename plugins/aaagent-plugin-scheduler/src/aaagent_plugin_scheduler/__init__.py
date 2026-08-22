"""Scheduler plugin — cron-style recurring / one-shot schedules.

Schedules are stored as JSON records in `data/scheduler/schedules.json`.
At trigger time the plugin emits `message_received` on the bus so the
Application's existing agent loop handles the LLM call, session
persistence, memory recall, and adapter reply end-to-end. No LLM code
is duplicated here.

Schedule permission model: each record carries `creator_user_id`.
`schedule_list` returns only schedules created by the user_id of the
current message (read from contextvars via `logctx.current_user_id`).
`schedule_remove` / `schedule_update` only allow the creator to mutate
their own schedules. Cross-user access is refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from croniter import croniter

from aaagent.core.logctx import current_user_id
from aaagent.core.message import Message
from aaagent.core.plugin import PluginContext, ToolPlugin
from aaagent.core.tool_registry import ToolRegistry

logger = logging.getLogger("aaagent.plugins.scheduler")

_DEFAULT_STORAGE = "data/scheduler/schedules.json"
_DEFAULT_TICK_S = 5
_MAX_FIRE_LOOKAHEAD = 3600  # skip schedules whose next_fire is far away


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _compute_next_cron(expr: str, base: datetime) -> datetime:
    itr = croniter(expr, base)
    return itr.get_next(datetime)


def _gen_id() -> str:
    return uuid.uuid4().hex[:10]


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


class _SchedulerStore:
    """JSON-backed persistent schedule store with cross-platform locking."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def _ensure_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _file_lock(self) -> Any:
        """Best-effort cross-platform file lock. Falls back to no-op."""
        lock_path = _lock_path(self._path)
        self._ensure_parent()
        try:
            if os.name == "nt":
                import msvcrt

                f = open(lock_path, "w")
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    f.close()
                    return None
                return f
            else:
                import fcntl

                f = open(lock_path, "w")
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except OSError:
                    f.close()
                    return None
                return f
        except Exception:
            return None

    @staticmethod
    def _release_file_lock(handle: Any) -> None:
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
        except Exception:
            pass

    def load_all(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self._path.exists():
                return []
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load schedules: %s", e)
                return []

    def save_all(self, schedules: list[dict[str, Any]]) -> None:
        with self._lock:
            self._ensure_parent()
            lock = self._file_lock()
            try:
                fd, tmp = tempfile.mkstemp(
                    dir=str(self._path.parent),
                    prefix=self._path.name + ".",
                    suffix=".tmp",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(schedules, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, self._path)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            finally:
                self._release_file_lock(lock)


def _validate_cron(expr: str) -> None:
    if not croniter.is_valid(expr):
        raise ValueError(f"无效 cron 表达式: {expr!r}")


def _validate_iso_at(at: str) -> datetime:
    try:
        return _parse_iso(at)
    except ValueError as e:
        raise ValueError(f"at 必须是 ISO datetime: {e}") from e


class SchedulerToolsPlugin(ToolPlugin):
    name = "scheduler"

    def __init__(self) -> None:
        self._bus: Any = None
        self._project_root: Path | None = None
        self._store: _SchedulerStore | None = None
        self._tick_seconds: int = _DEFAULT_TICK_S
        self._task: asyncio.Task | None = None

    def set_context(self, ctx: PluginContext) -> None:
        """Receive the framework-level handle.

        Replaces the legacy `set_application(app)` hook — the plugin no
        longer reaches into the Application object, it reads what it
        declared it needs from the controlled context handle.
        """
        self._bus = ctx.event_bus
        self._project_root = ctx.project_root

    def register(self, registry: ToolRegistry, config: dict[str, Any]) -> None:
        cfg = (config.get("tools") or {}).get("scheduler") or {}
        if not cfg.get("enabled", True):
            return
        storage_path = cfg.get("storage_path", _DEFAULT_STORAGE)
        from aaagent.core.paths import resolve_project_path

        project_root = self._project_root or Path.cwd()
        resolved = resolve_project_path(storage_path, project_root)
        self._store = _SchedulerStore(resolved)
        self._tick_seconds = int(cfg.get("tick_seconds", _DEFAULT_TICK_S))

        register_scheduler_tools(
            registry,
            store=self._store,
        )

    async def establish(self, registry: ToolRegistry, config: dict[str, Any]) -> None:
        """Spawn the background tick loop. Cancelled by close()."""
        if self._store is None:
            return
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("Scheduler tick loop started (every %ds)", self._tick_seconds)

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _tick_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._tick_seconds)
                await self._check_and_fire()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler tick failed")

    async def _check_and_fire(self) -> None:
        if self._store is None:
            return
        schedules = self._store.load_all()
        now = _now()
        dirty = False
        for s in schedules:
            if not s.get("enabled", True):
                continue
            next_iso = s.get("next_fire_at")
            if not next_iso:
                dirty |= self._recompute_next(s, now)
                continue
            try:
                next_dt = _parse_iso(next_iso)
            except ValueError:
                logger.warning("schedule %s: bad next_fire_at, recomputing", s.get("id"))
                dirty |= self._recompute_next(s, now)
                continue
            # Defensive: skip schedules more than 1h in the future (shouldn't happen
            # given we recompute on every fire, but catches clock skew / DST).
            if (next_dt - now).total_seconds() > _MAX_FIRE_LOOKAHEAD:
                continue
            if now < next_dt:
                continue
            try:
                await self._fire(s)
            except Exception:
                logger.exception(
                    "schedule %s: fire failed (creator=%s)",
                    s.get("id"),
                    s.get("creator_user_id"),
                )
            if s["type"] == "once":
                s["enabled"] = False
                s["last_fired_at"] = _iso(now)
                s.pop("next_fire_at", None)
                dirty = True
            else:
                s["last_fired_at"] = _iso(now)
                try:
                    s["next_fire_at"] = _iso(
                        _compute_next_cron(s["cron"], _now())
                    )
                except Exception:
                    logger.exception(
                        "schedule %s: recompute next failed, disabling", s.get("id")
                    )
                    s["enabled"] = False
                dirty = True
        if dirty:
            self._store.save_all(schedules)

    @staticmethod
    def _recompute_next(s: dict[str, Any], now: datetime) -> bool:
        try:
            if s["type"] == "once":
                fire_at = _parse_iso(s["at"])
                s["next_fire_at"] = _iso(fire_at)
            elif s["type"] == "recurring":
                s["next_fire_at"] = _iso(_compute_next_cron(s["cron"], now))
            return True
        except Exception:
            s["enabled"] = False
            return True

    async def _fire(self, sched: dict[str, Any]) -> None:
        if self._bus is None:
            logger.warning(
                "schedule %s: bus not set; cannot fire", sched.get("id")
            )
            return
        msg = Message(
            session_id=sched["session_id"],
            platform=sched["platform"],
            chat_id=sched["chat_id"],
            user_id=sched["user_id"],
            content=sched["prompt"],
            role="user",
            raw={"trigger": "scheduler", "schedule_id": sched["id"]},
        )
        await self._bus.emit("message_received", msg)
        logger.info(
            "schedule %s fired → %s/%s", sched["id"], sched["platform"], sched["chat_id"]
        )


def register_scheduler_tools(
    registry: ToolRegistry,
    store: _SchedulerStore,
) -> None:
    async def _current_user_id() -> str:
        # Tool handlers run inside _run_tool_loop which is wrapped by
        # set_context, so this contextvar reflects the inbound message.
        return current_user_id() or ""

    def _visible(schedules: list[dict[str, Any]], user_id: str
                 ) -> list[dict[str, Any]]:
        if not user_id:
            # No user context (CLI without user_id, or test env): return all.
            return schedules
        return [s for s in schedules if s.get("creator_user_id") == user_id]

    async def _create(args: dict[str, Any]) -> str:
        prompt = str(args.get("prompt", "")).strip()
        platform = str(args.get("platform", "")).strip()
        chat_id = str(args.get("chat_id", "")).strip()
        user_id = str(args.get("user_id", "")).strip()
        if not prompt or not platform or not chat_id or not user_id:
            return "错误：prompt、platform、chat_id、user_id 都不能为空。"
        sched_type = str(args.get("type", "")).strip().lower()
        if not sched_type:
            # Auto-detect from which field is supplied.
            if args.get("cron"):
                sched_type = "recurring"
            elif args.get("at"):
                sched_type = "once"
            else:
                return "错误：必须提供 cron（recurring）或 at（once）。"
        session_id = str(args.get("session_id", "")).strip() or f"{platform}-{chat_id}"

        record: dict[str, Any] = {
            "id": _gen_id(),
            "creator_user_id": await _current_user_id(),
            "platform": platform,
            "chat_id": chat_id,
            "user_id": user_id,
            "session_id": session_id,
            "prompt": prompt,
            "type": sched_type,
            "enabled": True,
            "created_at": _iso(_now()),
            "last_fired_at": None,
            "next_fire_at": None,
        }

        if sched_type == "recurring":
            cron = str(args.get("cron", "")).strip()
            try:
                _validate_cron(cron)
            except ValueError as e:
                return str(e)
            record["cron"] = cron
            record["next_fire_at"] = _iso(_compute_next_cron(cron, _now()))
        elif sched_type == "once":
            at = str(args.get("at", "")).strip()
            try:
                fire_at = _validate_iso_at(at)
            except ValueError as e:
                return str(e)
            if fire_at.tzinfo is None:
                fire_at = fire_at.replace(tzinfo=timezone.utc)
            record["at"] = _iso(fire_at)
            record["next_fire_at"] = _iso(fire_at)
        else:
            return f"错误：未知 type {sched_type!r}，只能是 recurring 或 once。"

        schedules = store.load_all()
        schedules.append(record)
        store.save_all(schedules)
        return (
            f"✅ 定时任务已创建：`{record['id']}` ({record['type']})\n"
            f"  首次触发：{record['next_fire_at']}\n"
            f"  推送到：{record['platform']}/{record['chat_id']}\n"
            f"  prompt：{prompt[:80]}"
        )

    async def _list(args: dict[str, Any]) -> str:
        uid = await _current_user_id()
        schedules = _visible(store.load_all(), uid)
        if not schedules:
            return "没有定时任务。"
        lines: list[str] = []
        for s in schedules:
            when = s.get("next_fire_at") or s.get("at") or "-"
            expr = s.get("cron") or s.get("at") or "-"
            enabled = "启用" if s.get("enabled", True) else "已停用"
            lines.append(
                f"- **{s['id']}** ({s['type']}, {enabled})\n"
                f"  下次：{when}\n"
                f"  表达式：{expr}\n"
                f"  推送：{s['platform']}/{s['chat_id']}\n"
                f"  prompt：{s['prompt'][:80]}"
            )
        return "\n".join(lines)

    async def _remove(args: dict[str, Any]) -> str:
        sid = str(args.get("schedule_id", "")).strip()
        if not sid:
            return "错误：缺少 schedule_id。"
        uid = await _current_user_id()
        schedules = store.load_all()
        kept = []
        removed: dict[str, Any] | None = None
        for s in schedules:
            if s.get("id") == sid:
                if uid and s.get("creator_user_id") != uid:
                    return f"错误：无权删除 schedule `{sid}`（仅创建者可删除）。"
                removed = s
                continue
            kept.append(s)
        if removed is None:
            return f"未找到 schedule `{sid}`。"
        store.save_all(kept)
        return f"✅ 已删除 schedule `{sid}`。"

    async def _update(args: dict[str, Any]) -> str:
        sid = str(args.get("schedule_id", "")).strip()
        if not sid:
            return "错误：缺少 schedule_id。"
        if "enabled" not in args:
            return "错误：需要 enabled 参数（true/false）。"
        enabled = bool(args.get("enabled"))
        uid = await _current_user_id()
        schedules = store.load_all()
        found: dict[str, Any] | None = None
        for s in schedules:
            if s.get("id") == sid:
                if uid and s.get("creator_user_id") != uid:
                    return f"错误：无权修改 schedule `{sid}`（仅创建者可修改）。"
                found = s
                break
        if found is None:
            return f"未找到 schedule `{sid}`。"
        found["enabled"] = enabled
        if enabled and not found.get("next_fire_at"):
            try:
                if found["type"] == "recurring":
                    found["next_fire_at"] = _iso(
                        _compute_next_cron(found["cron"], _now())
                    )
                elif found["type"] == "once":
                    found["next_fire_at"] = found["at"]
            except Exception as e:
                return f"错误：重新计算 next_fire_at 失败：{e}"
        store.save_all(schedules)
        state = "启用" if enabled else "停用"
        return f"✅ schedule `{sid}` 已{state}。"

    registry.register(
        name="schedule_create",
        description=(
            "创建定时任务。需要 platform/chat_id/user_id 三个投递地址，"
            "以及 cron（recurring：5 字段标准 cron）或 at（once：ISO datetime）"
            "二选一。session_id 可选，默认 = '{platform}-{chat_id}'。"
            "例：schedule_create(prompt='明天 9 点提醒我看天气', "
            "platform='feishu', chat_id='oc_xxx', user_id='u_1', cron='0 9 * * *')"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "触发时发给 LLM 的指令。"},
                "platform": {
                    "type": "string",
                    "description": "投递平台（feishu / cli ...）。",
                },
                "chat_id": {"type": "string", "description": "目标会话 ID。"},
                "user_id": {"type": "string", "description": "目标用户 ID。"},
                "session_id": {
                    "type": "string",
                    "description": "可选；默认 = '{platform}-{chat_id}'。",
                },
                "type": {
                    "type": "string",
                    "enum": ["recurring", "once"],
                    "description": "任务类型；省略时根据 cron/at 自动判断。",
                },
                "cron": {
                    "type": "string",
                    "description": "标准 5 字段 cron 表达式（分 时 日 月 周）。仅 recurring 用。",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime（如 2026-08-23T09:30:00）。仅 once 用。",
                },
            },
            "required": ["prompt", "platform", "chat_id", "user_id"],
        },
        handler=_create,
    )
    registry.register(
        name="schedule_list",
        description=(
            "列出当前用户创建的所有定时任务（含 disabled）。"
            "只显示调用方本人创建的；其他人创建的不可见。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_list,
    )
    registry.register(
        name="schedule_remove",
        description=(
            "删除一个定时任务。仅创建者可删除；schedule_id 从 schedule_list 获取。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "schedule_id": {"type": "string", "description": "任务 ID。"},
            },
            "required": ["schedule_id"],
        },
        handler=_remove,
    )
    registry.register(
        name="schedule_update",
        description=(
            "启用或停用一个定时任务。仅创建者可操作；传入 enabled=true/false。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "schedule_id": {"type": "string", "description": "任务 ID。"},
                "enabled": {
                    "type": "boolean",
                    "description": "true 启用，false 停用。",
                },
            },
            "required": ["schedule_id", "enabled"],
        },
        handler=_update,
    )