from __future__ import annotations

import pytest

from aaagent.core.bus import EventBus
from aaagent.core.commands import register_builtins
from aaagent_plugin_cliadapter import CliAdapter


class _ConsoleSpy:
    def __init__(self) -> None:
        self.prints: list[str] = []

    def print(self, *args, **kwargs) -> None:
        rendered = []
        for a in args:
            if hasattr(a, "plain"):
                rendered.append(a.plain)
            else:
                rendered.append(str(a))
        self.prints.append(" ".join(rendered))


def _make_adapter() -> tuple[CliAdapter, _ConsoleSpy]:
    bus = EventBus()
    adapter = CliAdapter({}, bus)
    spy = _ConsoleSpy()
    adapter._console = spy  # type: ignore[assignment]
    return adapter, spy


@pytest.mark.asyncio
async def test_cli_help_does_not_emit_message_received():
    adapter, spy = _make_adapter()
    captured = []

    async def capture(payload):
        captured.append(payload)

    adapter._bus.on("message_received", capture)

    # Application would normally listen on slash_command; here we
    # simulate it by emitting slash_reply + slash_unknown events so the
    # CLI subscriber path runs.
    await adapter._bus.emit(
        "slash_reply",
        {
            "platform": "cli",
            "session_id": adapter._session_id,
            "chat_id": adapter._session_id,
            "reply": "Available commands:",
            "suppressed": False,
        },
    )
    assert captured == []
    assert any("Available commands" in p for p in spy.prints)


@pytest.mark.asyncio
async def test_cli_slash_quit_sets_running_false():
    adapter, _ = _make_adapter()
    adapter._running = True  # normally set by start()
    await adapter._bus.emit(
        "slash_quit", {"platform": "cli", "session_id": adapter._session_id}
    )
    assert adapter._running is False


@pytest.mark.asyncio
async def test_cli_slash_quit_ignored_for_other_platform():
    adapter, _ = _make_adapter()
    adapter._running = True
    await adapter._bus.emit(
        "slash_quit", {"platform": "feishu", "session_id": "x"}
    )
    assert adapter._running is True


@pytest.mark.asyncio
async def test_cli_slash_session_switch_updates_session():
    adapter, _ = _make_adapter()
    original = adapter._session_id
    await adapter._bus.emit(
        "slash_session_switch",
        {
            "platform": "cli",
            "session_id": original,
            "chat_id": original,
            "new_session": "cli-foo",
        },
    )
    assert adapter._session_id == "cli-foo"


@pytest.mark.asyncio
async def test_cli_slash_unknown_prints_unknown_message():
    adapter, spy = _make_adapter()
    await adapter._bus.emit(
        "slash_unknown",
        {
            "platform": "cli",
            "session_id": adapter._session_id,
            "chat_id": adapter._session_id,
            "text": "/whatever",
        },
    )
    assert any("Unknown command: /whatever" in p for p in spy.prints)


@pytest.mark.asyncio
async def test_end_to_end_slash_command_flow():
    """Adapter emits slash_command, core registry resolves, replies flow back."""
    from aaagent.core.app import Application
    from aaagent.core.commands import SlashCommandRegistry
    from aaagent_plugin_markdownstore import MarkdownMemoryStore

    bus = EventBus()
    cfg_path = "_test_cli_e2e.yaml"

    import tempfile
    import os

    fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(
            "default_provider: x\n"
            "providers:\n  x: {type: custom, class: tests.conftest.FakeProvider, enabled: true}\n"
        )

    try:
        app = Application(
            config_path=cfg_path,
            bus=bus,
            memory=MarkdownMemoryStore(data_dir="data", base_path="."),
            tool_registry=None,
            providers={},
        )

        # Replace the tool registry's setup with a no-op since we don't need it.
        adapter = CliAdapter({}, bus)
        replies = []
        quits = []
        bus.on("slash_reply", lambda p: replies.append(p))
        bus.on("slash_quit", lambda p: quits.append(p))

        await bus.emit(
            "slash_command",
            {
                "text": "/help",
                "platform": "cli",
                "session_id": "cli-test",
                "chat_id": "cli-test",
            },
        )
        await bus.emit(
            "slash_command",
            {
                "text": "/quit",
                "platform": "cli",
                "session_id": "cli-test",
                "chat_id": "cli-test",
            },
        )

        assert len(replies) >= 1
        assert any("/help" in r["reply"] for r in replies)
        assert any("Bye" in r["reply"] for r in replies)
        assert len(quits) == 1
        assert quits[0]["platform"] == "cli"
    finally:
        os.unlink(cfg_path)