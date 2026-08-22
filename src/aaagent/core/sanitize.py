"""Secret redaction for LLM-bound and log-bound text.

`scrub()` removes high-risk credential patterns before text is fed back
into the LLM (via the tool loop) or written to a log handler. The
patterns cover:

* `sk-…` style API keys (OpenAI, Anthropic, MiniMax …)
* `key=value` / `key: value` style assignments for sensitive env vars
* HTTP `Authorization: Bearer …` headers
* Per-provider known env var assignments as a last-ditch safety net

The redaction is intentionally conservative: when in doubt we redact
the whole match rather than try to preserve a small visible suffix.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable


# Order matters: more specific patterns first so `sk-…` wins over the
# generic `key=value` sweep.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)xox[bprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    re.compile(
        r"(?i)(api[_-]?key|app[_-]?secret|access[_-]?token|password|secret)[^\n]{0,8}[:=][ ]*[\"']?[A-Za-z0-9._\-/+=]{6,}[\"']?"
    ),
    re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]+"),
    # Catch-all env-var assignment lines (e.g. "MINMAX_API_KEY=sk-..." or
    # "export MINMAX_API_KEY=sk-...") as a last line of defense.
    re.compile(r"(?i)\b(?:export\s+)?(?:[A-Z][A-Z0-9_]+_API_KEY|[A-Z][A-Z0-9_]+_TOKEN)\s*=\s*\S+"),
    re.compile(r"(?i)\b(?:export\s+)?(?:FEISHU_APP_ID|FEISHU_APP_SECRET)\s*=\s*\S+"),
]

_REPLACEMENT = "***"


def scrub(text: str | None) -> str:
    """Return `text` with credential patterns replaced by `***`.

    Non-string inputs are coerced to string. Returns the input unchanged
    if it is empty.
    """
    if not text:
        return text or ""
    out = text
    for pat in _PATTERNS:
        out = pat.sub(_REPLACEMENT, out)
    return out


class ScrubbingFormatter(logging.Formatter):
    """A `logging.Formatter` that scrubs secrets from the rendered record.

    Wraps another formatter's output through `scrub()` so the
    log-formatting logic (timestamps, level, context vars) is unchanged.
    If no `fmt` is provided we fall back to the default formatter layout.
    """

    def __init__(self, fmt: str | None = None, datefmt: str | None = None) -> None:
        super().__init__(fmt=fmt or "%(message)s", datefmt=datefmt)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return scrub(rendered)


def wrap_existing_handlers(
    extra_patterns: Iterable[re.Pattern[str]] | None = None,
) -> int:
    """Wrap every formatter on the root logger's handlers with scrubbing.

    Returns the number of handlers that were wrapped. Handlers whose
    formatter is already a `ScrubbingFormatter` are skipped to avoid
    double-wrapping. `extra_patterns` augments (does not replace) the
    built-in scrub patterns for the lifetime of these handlers.
    """
    if extra_patterns:
        _PATTERNS.extend(extra_patterns)
    root = logging.getLogger()
    wrapped = 0
    for handler in list(root.handlers):
        existing = handler.formatter
        if isinstance(existing, ScrubbingFormatter):
            continue
        if existing is None:
            new_fmt: logging.Formatter = ScrubbingFormatter()
        else:
            new_fmt = ScrubbingFormatter(fmt=existing._fmt, datefmt=existing.datefmt)
        handler.setFormatter(new_fmt)
        wrapped += 1
    return wrapped


__all__ = ["scrub", "ScrubbingFormatter", "wrap_existing_handlers"]
