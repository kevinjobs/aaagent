from __future__ import annotations

import os

import pytest

from aaagent.core.dotenv_io import DotenvStore


def test_set_creates_file_with_first_key(tmp_path):
    p = tmp_path / ".env"
    store = DotenvStore(p)
    assert store.set("FOO", "bar") == "set"
    assert p.read_text(encoding="utf-8") == 'FOO="bar"\n'
    assert p.with_suffix(".env.bak").exists() is False  # no previous file


def test_set_existing_key_same_value_is_noop(tmp_path):
    p = tmp_path / ".env"
    p.write_text('FOO="bar"\n', encoding="utf-8")
    store = DotenvStore(p)
    assert store.set("FOO", "bar") == "noop"
    # File content unchanged
    assert p.read_text(encoding="utf-8") == 'FOO="bar"\n'


def test_set_existing_key_different_value_overwrites(tmp_path):
    p = tmp_path / ".env"
    p.write_text('FOO="old"\n# a comment\nBAZ="keep"\n', encoding="utf-8")
    store = DotenvStore(p)
    assert store.set("FOO", "new") == "overwrite"
    text = p.read_text(encoding="utf-8")
    assert 'FOO="new"' in text
    assert 'BAZ="keep"' in text
    assert "a comment" in text
    # Backup written
    bak = tmp_path / ".env.bak"
    assert bak.exists()
    assert 'FOO="old"' in bak.read_text(encoding="utf-8")


def test_set_quotes_value_with_spaces_and_specials(tmp_path):
    p = tmp_path / ".env"
    store = DotenvStore(p)
    assert store.set("URL", "https://x.com/?a=1&b=2 'q' \"qq\"") == "set"
    text = p.read_text(encoding="utf-8")
    assert "URL=" in text
    # Round-trip via read
    assert store.read("URL") == "https://x.com/?a=1&b=2 'q' \"qq\""


def test_set_appends_after_blank_line_separator(tmp_path):
    p = tmp_path / ".env"
    p.write_text('FOO="bar"\n', encoding="utf-8")
    store = DotenvStore(p)
    store.set("NEW", "value")
    text = p.read_text(encoding="utf-8")
    assert 'FOO="bar"' in text
    assert 'NEW="value"' in text
    # A blank line separates the original from the appended entry
    lines = text.splitlines()
    assert lines == ['FOO="bar"', "", 'NEW="value"']


def test_unset_removes_entry(tmp_path):
    p = tmp_path / ".env"
    p.write_text('FOO="bar"\nBAZ="keep"\n', encoding="utf-8")
    store = DotenvStore(p)
    assert store.unset("FOO") is True
    assert store.read("FOO") is None
    assert store.read("BAZ") == "keep"
    assert store.unset("FOO") is False


def test_read_returns_none_for_missing(tmp_path):
    store = DotenvStore(tmp_path / ".env")
    assert store.read("NOPE") is None


def test_preserves_comments_and_blank_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# top-level comment\n"
        "\n"
        'A="1"\n'
        "  \n"
        "# inline comment\n"
        'B="2"\n',
        encoding="utf-8",
    )
    store = DotenvStore(p)
    store.set("A", "1")  # noop
    text = p.read_text(encoding="utf-8")
    # Comments and blank lines must survive a noop rewrite (parse +
    # reconstruct)
    assert "# top-level comment" in text
    assert "# inline comment" in text
    assert "\n\n" in text


def test_handles_export_prefix(tmp_path):
    p = tmp_path / ".env"
    p.write_text("export FOO=\"bar\"\n", encoding="utf-8")
    store = DotenvStore(p)
    assert store.read("FOO") == "bar"
    assert store.set("FOO", "bar") == "noop"


def test_handles_unquoted_and_single_quoted_values(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=plain\nB='single'\nC=\"double\"\n", encoding="utf-8")
    store = DotenvStore(p)
    assert store.read("A") == "plain"
    assert store.read("B") == "single"
    assert store.read("C") == "double"


@pytest.mark.skipif(os.name == "nt", reason="unix-only symlink behaviour")
def test_atomic_write_replaces_via_rename(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('OLD="v"\n', encoding="utf-8")
    store = DotenvStore(p)
    store.set("NEW", "x")
    assert p.read_text(encoding="utf-8").startswith('OLD="v"')
    # No leftover .tmp files
    leftovers = list(tmp_path.glob(".env.tmp"))
    assert leftovers == []
