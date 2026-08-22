from __future__ import annotations

import asyncio
import logging
import re
import shlex
import unicodedata
from typing import Any

from aaagent.core.plugin import ToolPlugin
from aaagent.core.sanitize import scrub

logger = logging.getLogger("aaagent.tools.shell")


def _normalize(cmd: str) -> str:
    s = unicodedata.normalize("NFKC", cmd)
    s = s.replace("\\", "")
    s = re.sub(r"\s+", " ", s)
    return s.lower().strip()


def _rules() -> list[tuple[set[str], Any, str]]:
    return [
        (
            {"rm"},
            lambda t: any(flag in t for flag in ("-rf", "-fr"))
            and any(x in ("/", "/*") for x in t),
            "rm -rf with root path",
        ),
        (
            {"dd"},
            lambda t: any(
                x.startswith("if=") and (x[3:].startswith("/") or ":\\" in x[3:])
                for x in t
            ),
            "dd with absolute input",
        ),
        (
            {"mkfs", "mkfs.bfs", "mkfs.cramfs", "mkfs.ext2", "mkfs.ext3", "mkfs.ext4",
             "mkfs.jfs", "mkfs.minix", "mkfs.msdos", "mkfs.ntfs", "mkfs.reiser4",
             "mkfs.vfat", "mkfs.xfs"},
            lambda t: True,
            "filesystem format",
        ),
        (
            {"format"},
            lambda t: True,
            "format command",
        ),
        (
            {"chmod"},
            lambda t: "777" in t and any(x in ("/", "/*") for x in t),
            "chmod 777 on root",
        ),
        (
            {"chown"},
            lambda t: any("root:root" in x for x in t)
            and any(x in ("/", "/*") for x in t),
            "chown root:root on root",
        ),
    ]


def _is_denied(command: str) -> str | None:
    normalized = _normalize(command)
    if ":(){" in command or ":(){" in normalized:
        return "fork bomb pattern"

    try:
        tokens = shlex.split(normalized)
    except ValueError:
        tokens = normalized.split()

    if not tokens:
        return None

    for head, predicate, description in _rules():
        for i, tok in enumerate(tokens):
            if tok in head and predicate(tokens[i:]):
                return description

    for i, tok in enumerate(tokens):
        if tok in {">", ">>"}:
            for target in tokens[i + 1:]:
                if (
                    target.startswith("/dev/sd")
                    or target.startswith("/dev/hd")
                    or target.startswith("/dev/nvme")
                ):
                    return "redirect to block device"

    return None


async def run_shell(args: dict[str, Any]) -> str:
    command = args["command"]
    timeout = args.get("timeout", 30)
    max_output = args.get("max_output", 4096)

    denied = _is_denied(command)
    if denied:
        return f"Error: command denied by safety policy ({denied})"

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Error: command timed out after {timeout}s"

        result_parts: list[str] = []
        if stdout:
            text = stdout.decode("utf-8", errors="replace")
            if len(text) > max_output:
                text = text[:max_output] + f"\n... (truncated, {len(text)} total bytes)"
            result_parts.append(f"[stdout]\n{text}")

        if stderr:
            text = stderr.decode("utf-8", errors="replace")
            if len(text) > max_output:
                text = text[:max_output] + f"\n... (truncated, {len(text)} total bytes)"
            result_parts.append(f"[stderr]\n{text}")

        if proc.returncode != 0:
            result_parts.insert(0, f"Exit code: {proc.returncode}")

        return "\n\n".join(result_parts) if result_parts else f"(no output, exit code {proc.returncode})"

    except FileNotFoundError:
        return f"Error: command not found: {shlex.split(command)[0]}"
    except Exception as e:
        return f"Error executing shell command: {scrub(str(e))}"


class ShellToolsPlugin(ToolPlugin):
    name = "shell"

    def register(self, registry: Any, config: dict[str, Any]) -> None:
        tools_cfg = config.get("tools", {}) if isinstance(config, dict) else {}
        shell_cfg = tools_cfg.get("shell", {}) if isinstance(tools_cfg, dict) else {}
        if not shell_cfg.get("enabled", True):
            return
        registry.register(
            name="run_shell",
            description=(
                "Execute a shell command. Use this to run CLI tools, scripts, "
                "or any shell operation. Has a timeout and output size limit."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30, max 120)",
                    },
                    "max_output": {
                        "type": "integer",
                        "description": "Maximum output characters (default 4096, max 32768)",
                    },
                },
                "required": ["command"],
            },
            handler=run_shell,
        )


__all__ = ["ShellToolsPlugin"]