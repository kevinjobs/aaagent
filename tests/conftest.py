import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest


@pytest.fixture
def tmp_memory_dir(tmp_path):
    """Temporary directory to use as MemoryStore base_path."""
    return tmp_path


@pytest.fixture
def fake_profile_provider():
    """A minimal fake LLM provider used by memory tests."""

    class _Fake:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            from aaagent.providers.base import ChatResponse

            self.calls += 1
            return ChatResponse(content="# 用户画像\n- consolidated")

    return _Fake()