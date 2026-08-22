"""Shared pytest fixtures for the aaagent workspace.

Pytest auto-loads `conftest.py` from the repo root for any test it
discovers, so core tests (`src/aaagent/tests/`) and every plugin's
tests (`plugins/aaagent-plugin-*/tests/`) see these fixtures
without each plugin having to redeclare them.

The fixtures are tiny — they exist so each plugin can ship its own
tests without re-implementing boilerplate. Real test logic lives in
the per-package test modules.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import pytest

from aaagent.core.types import ChatResponse
from aaagent.testing import FakeProvider


# Wire every plugin's `src/` into `sys.path` so the core's test suite
# can import plugins (e.g. `aaagent_plugin_shell.register` in
# `test_commands.py`) without the tests living inside the plugin.
_REPO_ROOT = Path(__file__).resolve().parent
for _plugin_src in sorted(
    _REPO_ROOT.glob("plugins/aaagent-plugin-*/src")
):
    sys.path.insert(0, str(_plugin_src))

# And make sure the core's own `src/` is on the path too — the uv
# workspace install already adds it, but keep it explicit for
# non-uv pytest invocations (e.g. a developer's local `pytest`).
sys.path.insert(0, str(_REPO_ROOT / "src"))


@pytest.fixture
def tmp_memory_dir(tmp_path):
    """Temporary directory to use as `MemoryStore` `base_path`."""
    return tmp_path


@pytest.fixture
def fake_provider() -> FakeProvider:
    """Empty FakeProvider — tests push responses via `provider.push(...)`."""
    return FakeProvider()


@pytest.fixture
def fake_profile_provider():
    """Provider that returns a consolidated profile when asked.

    Used by `aaagent-plugin-markdownstore` tests for the
    `maybe_consolidate_profile` path. Returns a one-shot object whose
    `.calls` counter tells you how many LLM round-trips were made.
    """

    class _Fake:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            return ChatResponse(content="# 用户画像\n- consolidated")

    return _Fake()


__all__ = ["FakeProvider"]