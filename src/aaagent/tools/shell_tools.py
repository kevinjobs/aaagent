from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

logger = logging.getLogger("aaagent.tools.shell")

_DENY_LIST = [
    "rm -rf /",
    "rm -rf /*",
    "dd if=",
    "mkfs.",
    "format ",
    ":(){ :|:& };:",
    "> /dev/sda",
    "> /dev/hda",
    "chmod 777 /",
    "chown root:root /",
]


def _is_denied(command: str) -> str | None:
    cmd_lower = command.lower().strip()
    for denied in _DENY_LIST:
        if denied.lower() in cmd_lower:
            return denied
    return None


async def run_shell(args: dict[str, Any]) -> str:
    command = args["command"]
    timeout = args.get("timeout", 30)
    max_output = args.get("max_output", 4096)

    denied = _is_denied(command)
    if denied:
        return f"Error: command denied (matches blacklist pattern: '{denied}')"

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
        return f"Error executing shell command: {e}"


def register_shell_tools(registry: Any) -> None:
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
