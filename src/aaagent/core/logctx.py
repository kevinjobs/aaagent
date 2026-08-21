from __future__ import annotations

import contextvars
import logging
from typing import Any

_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aa_session_id", default=""
)
_platform: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aa_platform", default=""
)
_chat_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aa_chat_id", default=""
)


class ContextFilter(logging.Filter):
    """Injects session_id / platform / chat_id from contextvars into each LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _session_id.get()
        record.platform = _platform.get()
        record.chat_id = _chat_id.get()
        return True


def install_logging(level: str = "INFO") -> None:
    """Install default logging with ContextFilter attached to a stream handler."""
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(session_id)s/%(platform)s] %(name)s: %(message)s"
    )
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def set_context(
    session_id: str = "",
    platform: str = "",
    chat_id: str = "",
) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    """Set contextvars and return tokens for later reset."""
    return (
        _session_id.set(session_id),
        _platform.set(platform),
        _chat_id.set(chat_id),
    )


def reset_context(
    tokens: tuple[contextvars.Token, contextvars.Token, contextvars.Token],
) -> None:
    sid_t, plt_t, cid_t = tokens
    _session_id.reset(sid_t)
    _platform.reset(plt_t)
    _chat_id.reset(cid_t)