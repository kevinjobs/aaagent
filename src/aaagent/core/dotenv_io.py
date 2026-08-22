"""Atomic, comment-preserving write of `.env` files.

The `/model -new` slash command writes new API keys into `.env` rather
than into `config.yaml` so secrets stay out of version-controlled
config. Each `set()` call is idempotent and atomic:

1. Read the existing file (if any) into an ordered dict, preserving
   blank lines and `# comment` lines as opaque line strings.
2. Compare the new (key, value) pair to the existing one. If equal,
   return `"noop"`; if the key exists with a different value, return
   `"overwrite"` (and the caller is expected to log a WARN before
   proceeding); if absent, return `"set"`.
3. Rewrite to `<path>.tmp` and `os.replace` it into place. A
   `<path>.bak` snapshot of the previous file is left as a safety net.

Comment lines and blank lines are preserved verbatim, which means
operators can organise `.env` by hand (e.g. `# OpenAI` section
header) without losing the structure after a slash-command edit.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SetResult = Literal["set", "overwrite", "noop"]


@dataclass
class _Line:
    """One logical line of a `.env` file.

    Either a `key=value` entry, a comment/blank line, or a malformed
    line (preserved verbatim). Order matters: when we rewrite the
    file we walk the original lines in order and replace the matching
    key's line in-place, appending a new line at the end if the key
    was absent.
    """

    kind: Literal["entry", "raw"]
    raw: str
    key: str | None = None
    value: str | None = None


_ENTRY_RE = re.compile(
    r"^\s*(?:export\s+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*=\s*"
    r"(?P<value>.*?)\s*$"
)


def _parse_line(text: str) -> _Line:
    """Classify a single line as `entry` or `raw`."""
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return _Line(kind="raw", raw=text)
    m = _ENTRY_RE.match(text)
    if not m:
        return _Line(kind="raw", raw=text)
    key = m.group("key")
    raw_value = m.group("value")
    # Strip matching surrounding quotes if present
    value = raw_value
    if len(value) >= 2 and value[0] == value[-1] and value[0] == '"':
        value = _unescape_value(value[1:-1])
    elif len(value) >= 2 and value[0] == value[-1] and value[0] == "'":
        # Single quotes — no escape processing (POSIX shell semantics)
        value = value[1:-1]
    return _Line(kind="entry", raw=text, key=key, value=value)


def _escape_value(value: str) -> str:
    """Quote a value so it round-trips through `_parse_line` lossless.

    Always uses double quotes and escapes embedded quotes / backslashes
    / newlines. This is the conservative choice; we never rely on
    shell-specific escape rules.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _unescape_value(value: str) -> str:
    """Inverse of `_escape_value` for double-quoted strings."""
    out: list[str] = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "t":
                out.append("\t")
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class DotenvStore:
    """Persistent view of a `.env` file with atomic `set` / `unset`."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _read_lines(self) -> list[_Line]:
        if not self._path.exists():
            return []
        with open(self._path, encoding="utf-8") as f:
            return [_parse_line(line) for line in f.readlines()]

    def read(self, key: str) -> str | None:
        """Return the current value of `key`, or `None` if absent."""
        for line in self._read_lines():
            if line.kind == "entry" and line.key == key:
                return line.value
        return None

    def set(self, key: str, value: str) -> SetResult:
        """Insert or update `key=value`. Returns the change classification.

        * `"noop"`     — the key already had the same value
        * `"overwrite"` — the key existed with a different value (now
          replaced; per Q1 the policy is to overwrite unconditionally,
          the caller decides whether to surface a WARN before calling)
        * `"set"`      — the key did not exist
        """
        lines = self._read_lines()
        existing_value = self.read(key)
        if existing_value == value:
            return "noop"

        new_entry = f"{key}={_escape_value(value)}\n"
        result: SetResult = "set"
        replaced = False
        for i, line in enumerate(lines):
            if line.kind == "entry" and line.key == key:
                lines[i] = _Line(kind="entry", raw=new_entry, key=key, value=value)
                replaced = True
                result = "overwrite"
                break

        if not replaced:
            # Ensure file ends with a blank line before appending if
            # the last existing line is non-blank; keeps the file
            # readable when edited by hand.
            if lines and lines[-1].kind == "entry":
                lines.append(_Line(kind="raw", raw="\n"))
            lines.append(_Line(kind="entry", raw=new_entry, key=key, value=value))

        self._write(lines)
        return result

    def unset(self, key: str) -> bool:
        """Remove `key` from the file. Returns True if it was present."""
        lines = self._read_lines()
        new_lines = [line for line in lines if not (line.kind == "entry" and line.key == key)]
        if len(new_lines) == len(lines):
            return False
        self._write(new_lines)
        return True

    def _write(self, lines: list[_Line]) -> None:
        if self._path.exists():
            backup = self._path.with_name(self._path.name + ".bak")
            shutil.copy2(self._path, backup)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line.raw if line.raw.endswith("\n") else line.raw + "\n")
        os.replace(tmp, self._path)


__all__ = ["DotenvStore", "SetResult"]
