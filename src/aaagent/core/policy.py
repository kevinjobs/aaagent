"""Path-policy helpers shared by tool plugins.

`limits.protected_paths` is a list of glob patterns (e.g.
`config.yaml`, `.env`, `*.pem`, `**/id_rsa*`). Every filesystem
write the agent attempts — through `write_file`, `run_shell` with
`>` / `tee`, etc. — must run those paths through
`is_protected_target()` before the action is taken.

The check is intentionally filesystem-local: we don't try to
resolve symlinks or evaluate shell redirection logic. That keeps
the helper usable both from filetools (which receives explicit
paths) and from shelltools (which sees a flat command string and
must tokenise it).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Iterable


def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a shell-style glob into a compiled regex.

    Supports `*` (any chars except `/`), `**` (any chars including
    `/`, also matches zero directories), and `?`. Pure filename
    patterns (no slash) match against the basename only; patterns
    with a slash match against the full normalised path. The
    `**` token is also accepted without a trailing slash so
    `**/id_rsa*` matches a bare `id_rsa` in the root as well as one
    nested deep in a sub-tree.
    """
    p = pattern.replace("\\", "/")
    has_slash = "/" in p
    parts = re.split(r"(/|\*\*|[?*])", p)
    out: list[str] = []
    i = 0
    while i < len(parts):
        chunk = parts[i]
        if i + 1 < len(parts) and parts[i + 1] == "**":
            # `**` (optionally followed by `/`) — matches zero or
            # more directory segments. `re.split` leaves an empty
            # string between `**` and a `/` separator that follows,
            # so we skip both.
            out.append("(?:.*/)?")
            i += 2
            while i < len(parts) and parts[i] in ("", "/"):
                i += 1
            continue
        if chunk == "*":
            out.append("[^/]*")
        elif chunk == "**":
            out.append(".*")
        elif chunk == "?":
            out.append("[^/]")
        elif chunk == "/":
            out.append("/")
        else:
            out.append(re.escape(chunk))
        i += 1
    body = "".join(out)
    if has_slash:
        return re.compile("^" + body + "$")
    return re.compile("^" + body + "$")


def _normalise(path_str: str) -> str:
    """Convert a path string to a forward-slash form for matching."""
    return str(Path(path_str)).replace("\\", "/")


def is_protected_target(
    target: str, patterns: Iterable[str], base: Path | None = None
) -> bool:
    """Return True if `target` matches any glob in `patterns`.

    `base` is used to resolve relative paths before matching (defaults
    to the current working directory). The match runs against the
    normalised full path, so `config.yaml` matches both
    `/repo/config.yaml` and `C:/repo/config.yaml`.
    """
    if not patterns:
        return False
    p = Path(target)
    if not p.is_absolute() and base is not None:
        p = (base / p).resolve()
    else:
        p = p.resolve()
    normalised_full = _normalise(str(p))
    normalised_base = _normalise(Path(p).name)
    for raw in patterns:
        if not isinstance(raw, str) or not raw:
            continue
        compiled = _compile_glob(raw)
        if compiled.match(normalised_full) or compiled.match(normalised_base):
            return True
    return False


def extract_shell_paths(command: str) -> list[str]:
    """Pull file-path-like tokens out of a shell command string.

    Heuristic, not a parser. We split on whitespace and treat tokens
    containing a path separator or a recognised extension as
    candidate paths. Redirection targets (the token after `>` / `>>`)
    are included. Quoted strings are stripped of their surrounding
    quotes before the test runs.
    """
    import shlex

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()

    out: list[str] = []
    for i, tok in enumerate(tokens):
        # Skip the redirection operator itself
        if tok in {">", ">>", "<", "2>", "2>>", "&>", "&>>"}:
            continue
        # The token right after a redirection operator is a target
        if i > 0 and tokens[i - 1] in {">", ">>", "<", "2>", "2>>", "&>", "&>>"}:
            out.append(tok)
            continue
        # Anything with a path separator OR a leading dot OR a file
        # extension OR a recognised absolute-path prefix counts.
        if (
            "/" in tok
            or "\\" in tok
            or tok.startswith(".")
            or tok.startswith("~")
            or re.search(r"\.[A-Za-z0-9]{1,5}$", tok)
        ):
            out.append(tok)
    return out


__all__ = ["is_protected_target", "extract_shell_paths"]
