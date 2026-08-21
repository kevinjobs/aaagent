from __future__ import annotations

import os
import re

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_env(value: object) -> str:
    """Expand ${ENV_VAR} placeholders in a config string from the environment.

    Non-string values are returned unchanged. If the env var is missing, an
    empty string is substituted. Whitespace around the var name is tolerated.
    """
    if not isinstance(value, str):
        return value  # type: ignore[return-value]
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def resolve_env_dict(cfg: dict) -> dict:
    """Return a shallow copy of cfg with string values env-resolved."""
    return {k: resolve_env(v) for k, v in cfg.items()}