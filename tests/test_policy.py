from __future__ import annotations

from pathlib import Path

import pytest

from aaagent.core.policy import (
    extract_shell_paths,
    is_protected_target,
)


def test_is_protected_target_simple_filename(tmp_path):
    (tmp_path / "config.yaml").write_text("x", encoding="utf-8")
    assert is_protected_target("config.yaml", ["config.yaml"], base=tmp_path) is True


def test_is_protected_target_unmatched_returns_false(tmp_path):
    assert is_protected_target("README.md", ["config.yaml"], base=tmp_path) is False


def test_is_protected_target_glob_star(tmp_path):
    (tmp_path / "server.pem").write_text("x", encoding="utf-8")
    assert is_protected_target("server.pem", ["*.pem"], base=tmp_path) is True


def test_is_protected_target_glob_doublestar(tmp_path):
    nested = tmp_path / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / "id_rsa").write_text("x", encoding="utf-8")
    assert is_protected_target("sub/deep/id_rsa", ["**/id_rsa*"], base=tmp_path) is True


def test_is_protected_target_empty_patterns(tmp_path):
    assert is_protected_target("anything", [], base=tmp_path) is False


def test_is_protected_target_absolute_path(tmp_path):
    p = tmp_path / ".env"
    p.write_text("x", encoding="utf-8")
    assert is_protected_target(str(p), [".env"], base=tmp_path) is True


def test_is_protected_target_dotenv_extension(tmp_path):
    (tmp_path / "config.env").write_text("x", encoding="utf-8")
    # dot glob matches anything starting with `.env`; "config.env"
    # does NOT start with `.` so it should NOT match `.env*` only if
    # we use the leading-dot semantics. `*` allows any prefix.
    assert is_protected_target("config.env", ["*.env"], base=tmp_path) is True


def test_is_protected_target_windows_separators(tmp_path):
    """A glob pattern with forward slashes must still match a path
    stringified with backslashes (Windows)."""
    p = tmp_path / "config.yaml"
    p.write_text("x", encoding="utf-8")
    backslash_form = str(p).replace("/", "\\")
    assert is_protected_target(backslash_form, ["config.yaml"], base=tmp_path) is True


def test_is_protected_target_question_mark(tmp_path):
    (tmp_path / "a.pem").write_text("x", encoding="utf-8")
    assert is_protected_target("a.pem", ["?.pem"], base=tmp_path) is True
    assert is_protected_target("ab.pem", ["?.pem"], base=tmp_path) is False


def test_is_protected_target_skips_non_string_patterns(tmp_path):
    """Defensive: ignore None / int entries in the patterns list."""
    assert is_protected_target("anything", [None, 42, ""], base=tmp_path) is False


# ----- extract_shell_paths ----------------------------------------------------


def test_extract_shell_paths_simple_command():
    out = extract_shell_paths("cat config.yaml")
    assert "config.yaml" in out


def test_extract_shell_paths_absolute_path():
    out = extract_shell_paths("rm /etc/passwd")
    assert "/etc/passwd" in out


def test_extract_shell_paths_redirect_target():
    out = extract_shell_paths("echo secret > .env")
    assert ".env" in out
    # The `>` operator itself should NOT be in the output
    assert ">" not in out


def test_extract_shell_paths_append_redirect():
    out = extract_shell_paths("echo data >> config.yaml")
    assert "config.yaml" in out


def test_extract_shell_paths_stderr_redirect():
    out = extract_shell_paths("cmd 2> error.log")
    assert "error.log" in out


def test_extract_shell_paths_quoted_path():
    out = extract_shell_paths('cat "config.yaml"')
    assert "config.yaml" in out


def test_extract_shell_paths_dot_relative():
    out = extract_shell_paths("ls ./data")
    assert "./data" in out


def test_extract_shell_paths_skips_flags():
    # Plain flag tokens should not be picked up as paths
    out = extract_shell_paths("ls -la /tmp")
    assert "-la" not in out
    assert "/tmp" in out


def test_extract_shell_paths_ignores_pipe_without_target():
    # Bare-word arguments with no path separator / extension are NOT
    # treated as candidate paths — they're variable names, not
    # files. Use an obvious extension to verify the heuristic picks
    # up realistic targets.
    out = extract_shell_paths("cat data.txt | grep info.txt")
    assert "data.txt" in out
    assert "info.txt" in out


def test_extract_shell_paths_tilde():
    out = extract_shell_paths("cp foo ~/backup.yaml")
    assert "~/backup.yaml" in out
