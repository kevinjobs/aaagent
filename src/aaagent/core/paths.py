"""Resolve path-typed config values relative to the project root.

The project root is defined as the directory containing
`config.yaml`. Every relative path in `config.yaml` — `paths.dotenv`,
`memory.data_dir`, `tools.allowed_dirs`, `limits.protected_paths`,
… — is rewritten to an absolute path anchored at this directory at
load time.

Why: previously each of these was resolved against `os.getcwd()` at
the moment the path was first used. That made behaviour depend on
where the operator happened to launch the binary from — fine for the
common case but a sharp edge when aaagent is driven by a remote
adapter (Feishu, systemd) whose CWD is unpredictable. Anchoring on
`config.yaml.parent` removes the CWD dependency: the same config
file always describes the same paths no matter who launched the
process.

Path-typed keys are registered in `_PATH_KEYS` (mapping of YAML path
to "scalar" | "list"). When a new config option that accepts a path
is added, list it here so the resolution semantics stay uniform.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

_PATH_KEYS: dict[tuple[str, ...], str] = {
    ("paths", "dotenv"): "scalar",
    ("memory", "base_path"): "scalar",
    ("memory", "data_dir"): "scalar",
    ("tools", "allowed_dirs"): "list",
    ("tools", "skills", "skills_dir"): "scalar",
    ("limits", "protected_paths"): "list",
}


def resolve_project_path(value: str | Path, project_root: Path) -> Path:
    """Expand `~`, then resolve relative paths against `project_root`.

    Absolute paths are returned untouched (after symlink resolution
    via `Path.resolve()`). This is the only place where relative
    paths from config.yaml are turned into absolute ones.
    """
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (project_root / p).resolve()
    return p


def _walk_set(
    cfg: Any, key_path: Iterable[str], value: Any
) -> None:
    """Set `cfg[key_path[0]][key_path[1]]... = value` in-place.

    Creates intermediate dicts as needed. If the parent isn't a
    dict/mapping the call is a no-op (defensive — shouldn't happen
    with a well-formed config).
    """
    keys = list(key_path)
    if not keys:
        return
    node: Any = cfg
    for k in keys[:-1]:
        nxt = node.get(k) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            return
        node = nxt
    if isinstance(node, dict):
        node[keys[-1]] = value


def _walk_get(cfg: Any, key_path: Iterable[str]) -> Any:
    node: Any = cfg
    for k in key_path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def resolve_all_paths(cfg: dict, project_root: Path) -> None:
    """Walk `_PATH_KEYS` and rewrite each entry to an absolute path.

    Mutates `cfg` in place. Strings that are already absolute are
    re-resolved (cheap, normalises `..` and `~`); relative strings
    become `project_root / value`. Missing keys are left alone.
    """
    for key_path, kind in _PATH_KEYS.items():
        cur = _walk_get(cfg, key_path)
        if cur is None:
            continue
        if kind == "scalar":
            if isinstance(cur, str):
                _walk_set(cfg, key_path, str(resolve_project_path(cur, project_root)))
        elif kind == "list":
            if isinstance(cur, list):
                resolved = [
                    str(resolve_project_path(p, project_root))
                    if isinstance(p, str) else p
                    for p in cur
                ]
                _walk_set(cfg, key_path, resolved)


__all__ = ["resolve_project_path", "resolve_all_paths", "_PATH_KEYS"]
