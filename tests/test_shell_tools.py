from __future__ import annotations

import pytest

from aaagent_plugin_shelltools import _is_denied, run_shell


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


# ----------------------------------------------------------------------
# protected_paths gating
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_shell_blocks_redirect_to_protected_path():
    out = await run_shell(
        {"command": "echo evil > config.yaml"},
        protected_patterns=["config.yaml"],
    )
    assert "protected" in out.lower()
    assert "blocked" in out.lower()


@pytest.mark.asyncio
async def test_run_shell_blocks_append_redirect_to_protected():
    out = await run_shell(
        {"command": "echo x >> config.yaml"},
        protected_patterns=["config.yaml"],
    )
    assert "protected" in out.lower()


@pytest.mark.asyncio
async def test_run_shell_blocks_cat_into_protected_path():
    out = await run_shell(
        {"command": "cat foo > .env"},
        protected_patterns=[".env"],
    )
    assert "protected" in out.lower()


@pytest.mark.asyncio
async def test_run_shell_allows_unrelated_targets():
    """Without protected_patterns or with non-matching ones, the
    command runs through to the deny rules and then the executor."""
    out = await run_shell(
        {"command": "echo done > result.txt"},
        protected_patterns=["config.yaml", ".env"],
    )
    # The command was NOT blocked by the protected-paths gate.
    # (echo redirects stdout to a file, so we won't see "done" in
    # the captured output — that's a property of echo, not a sign
    # the gate fired.)
    assert "protected" not in out.lower()
    assert "blocked" not in out.lower()


@pytest.mark.asyncio
async def test_run_shell_protected_paths_glob():
    out = await run_shell(
        {"command": "cp foo key.pem"},
        protected_patterns=["*.pem"],
    )
    assert "protected" in out.lower()


@pytest.mark.asyncio
async def test_run_shell_no_protected_patterns_no_op():
    """When protected_patterns is None/empty, behaviour is unchanged."""
    out = await run_shell(
        {"command": "echo a > config.yaml"},
        protected_patterns=None,
    )
    assert "protected" not in out.lower()