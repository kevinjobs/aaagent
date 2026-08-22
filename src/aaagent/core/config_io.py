"""Atomic, comment-preserving read/write of config.yaml.

Built on ruamel.yaml's round-trip mode so existing comments, blank
lines, and key ordering survive a save triggered by runtime slash
commands like `/model -new`. Saves are atomic (write to .tmp then
rename) and a `.bak` copy of the previous file is left in place as a
safety net.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


class ConfigStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Any:
        """Round-trip parse. Returns a ruamel CommentMap / dict-like."""
        from ruamel.yaml import YAML

        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        with open(self._path, encoding="utf-8") as f:
            data = yaml.load(f)
        return data if data is not None else _empty_doc()

    def save(self, cfg: Any) -> None:
        """Atomic write. Backs up the previous file to <path>.bak first."""
        from ruamel.yaml import YAML

        if self._path.exists():
            backup = self._path.with_suffix(self._path.suffix + ".bak")
            shutil.copy2(self._path, backup)

        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        yaml.default_flow_style = False
        yaml.indent(mapping=2, sequence=4, offset=2)
        yaml.width = 4096

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f)
        os.replace(tmp, self._path)


def _empty_doc() -> Any:
    from ruamel.yaml.comments import CommentedMap

    return CommentedMap()


__all__ = ["ConfigStore"]