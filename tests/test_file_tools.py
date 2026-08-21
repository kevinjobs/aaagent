from __future__ import annotations

import pytest

from aaagent_plugin_filetools import (
    _ensure_allowed,
    list_dir,
    read_file,
    write_file,
)


def test_ensure_allowed_raises_when_no_allowed_dirs():
    with pytest.raises(PermissionError, match="no allowed_dirs"):
        _ensure_allowed("/tmp/foo", None)


def test_ensure_allowed_raises_when_empty_allowed_dirs():
    with pytest.raises(PermissionError, match="no allowed_dirs"):
        _ensure_allowed("/tmp/foo", [])


def test_ensure_allowed_allows_subpath(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    sub = base / "sub"
    sub.mkdir()
    result = _ensure_allowed(str(sub), [str(base)])
    assert result == str(sub.resolve())


def test_ensure_allowed_rejects_outside_path(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PermissionError, match="not in allowed directories"):
        _ensure_allowed(str(outside), [str(base)])


@pytest.mark.asyncio
async def test_read_file_inside_allowed(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    target = base / "f.txt"
    target.write_text("hello", encoding="utf-8")
    out = await read_file({"path": str(target)}, [str(base)])
    assert out == "hello"


@pytest.mark.asyncio
async def test_read_file_outside_allowed(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    out = await read_file({"path": str(outside)}, [str(base)])
    assert "PermissionError" in out or "not in allowed" in out


@pytest.mark.asyncio
async def test_write_file_creates_parent(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    target = base / "sub" / "f.txt"
    out = await write_file({"path": str(target), "content": "x"}, [str(base)])
    assert "Successfully wrote" in out
    assert target.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_list_dir_lists_entries(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "a.txt").write_text("x", encoding="utf-8")
    (base / "b").mkdir()
    out = await list_dir({"path": str(base)}, [str(base)])
    assert "a.txt" in out
    assert "b/" in out