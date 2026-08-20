from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from aaagent.adapters.base import IMAdapter
from aaagent.core.bus import EventBus
from aaagent.core.message import Message


class CliAdapter(IMAdapter):
    name = "cli"

    def __init__(self, config: dict[str, Any], bus: EventBus) -> None:
        super().__init__(config, bus)
        self._console = Console()
        self._running = False
        self._session_id = "cli-default"
        self._user_id = "cli-user"
        self._bus = bus
        self._bus.on("message_to_send", self._on_message_to_send)
        self._bus.on("tool_start", self._on_tool_start)
        self._bus.on("tool_result", self._on_tool_result)

    async def start(self) -> None:
        self._running = True
        self._console.print(
            Panel("aaagent CLI chat mode", style="bold cyan", subtitle="Type /help for commands")
        )
        await self._read_loop()

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: Message) -> None:
        self._print_assistant(msg.content)

    async def _on_message_to_send(self, msg: Message) -> None:
        if msg.platform == "cli":
            self._print_assistant(msg.content)

    async def _on_tool_start(self, data: dict[str, Any]) -> None:
        if data.get("platform") != "cli":
            return
        turn = data["turn"]
        for tc in data["tool_calls"]:
            try:
                import json
                args = json.loads(tc.arguments)
            except (json.JSONDecodeError, TypeError):
                args = tc.arguments
            self._console.print(
                Panel(
                    f"[bold yellow]🔧 {tc.name}[/bold yellow]\n\n{args}",
                    border_style="yellow",
                    title=f"Tool Call (turn {turn})",
                    title_align="left",
                )
            )

    async def _on_tool_result(self, data: dict[str, Any]) -> None:
        if data.get("platform") != "cli":
            return
        result = data["result"]
        is_error = result.startswith("Error:")
        style = "red" if is_error else "green"
        label = f"Result ({data['tool_name']})"
        content = result[:2000]

        if len(content) > 500 and not is_error:
            self._console.print(
                Panel(
                    Syntax(content, "text", theme="monokai", word_wrap=True),
                    border_style=style,
                    title=label,
                    title_align="left",
                )
            )
        else:
            self._console.print(
                Panel(
                    content,
                    border_style=style,
                    title=label,
                    title_align="left",
                )
            )

    async def _read_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, self._read_input)
                if line is None:
                    continue
                line = line.strip()
                if not line:
                    continue

                if line.startswith("/"):
                    if self._handle_command(line):
                        continue

                msg = Message(
                    session_id=self._session_id,
                    platform="cli",
                    chat_id=self._session_id,
                    user_id=self._user_id,
                    content=line,
                    role="user",
                )
                await self._bus.emit("message_received", msg)
            except (EOFError, KeyboardInterrupt):
                self._running = False
                break

    def _read_input(self) -> str | None:
        try:
            return self._console.input("[bold green]You>[/] ")
        except (EOFError, KeyboardInterrupt):
            raise

    def _handle_command(self, cmd: str) -> bool:
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if command == "/quit" or command == "/exit":
            self._running = False
            return True
        elif command == "/session":
            if arg:
                self._session_id = f"cli-{arg}"
                self._console.print(f"[dim]Switched to session: {self._session_id}[/]")
            else:
                self._console.print(f"[dim]Current session: {self._session_id}[/]")
            return True
        elif command == "/help":
            self._console.print(
                "[dim]/quit, /exit - Exit chat\n"
                "/session <name> - Switch session\n"
                "/help - Show this help[/]"
            )
            return True
        else:
            self._console.print(f"[dim]Unknown command: {command}[/]")
            return True

    def _print_assistant(self, content: str) -> None:
        self._console.print()
        self._console.print(Text("Assistant>", style="bold blue"), end=" ")
        self._console.print(Markdown(content))
        self._console.print()
