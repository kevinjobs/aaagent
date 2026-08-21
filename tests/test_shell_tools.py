from __future__ import annotations

import pytest

from aaagent.tools.shell_tools import _is_denied, run_shell


DENIED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "r\\m -rf /",
    "echo x | rm -rf /",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda",
    "chmod 777 /",
    "chown root:root /",
    "echo data > /dev/sda",
    ":(){:|:&};:",
]


ALLOWED_COMMANDS = [
    "ls -la",
    "echo hello",
    "cat file.txt",
    "python --version",
    "git status",
    "rm file.txt",
    "rm -rf build/",
    "chmod 644 file.txt",
    "dd if=image.bin of=output.bin",
    "echo done > result.txt",
]


@pytest.mark.parametrize("cmd", DENIED_COMMANDS)
def test_is_denied_blocks_dangerous(cmd):
    assert _is_denied(cmd) is not None, f"expected denial for: {cmd}"


@pytest.mark.parametrize("cmd", ALLOWED_COMMANDS)
def test_is_denied_allows_safe(cmd):
    assert _is_denied(cmd) is None, f"unexpected denial for: {cmd}"


@pytest.mark.asyncio
async def test_run_shell_blocks_dangerous_command():
    out = await run_shell({"command": "rm -rf /"})
    assert "denied by safety policy" in out


@pytest.mark.asyncio
async def test_run_shell_executes_safe_command():
    out = await run_shell({"command": "echo hello"})
    assert "hello" in out