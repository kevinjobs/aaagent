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
_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aa_user_id", default=""
)


class ContextFilter(logging.Filter):
    """Injects session_id / platform / chat_id / user_id from contextvars."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _session_id.get()
        record.platform = _platform.get()
        record.chat_id = _chat_id.get()
        record.user_id = _user_id.get()
        return True


def install_logging(level: str = "INFO") -> None:
    """Install default logging with ContextFilter attached to a stream handler."""
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(session_id)s/%(platform)s] %(name)s: %(message)s"
    )
    handler = logging.StreamHandler()
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
    user_id: str = "",
) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token, contextvars.Token]:
    """Set contextvars and return tokens for later reset."""
    return (
        _session_id.set(session_id),
        _platform.set(platform),
        _chat_id.set(chat_id),
        _user_id.set(user_id),
    )


def reset_context(
    tokens: tuple[
        contextvars.Token, contextvars.Token, contextvars.Token, contextvars.Token
    ],
) -> None:
    sid_t, plt_t, cid_t, uid_t = tokens
    _session_id.reset(sid_t)
    _platform.reset(plt_t)
    _chat_id.reset(cid_t)
    _user_id.reset(uid_t)


def current_user_id() -> str:
    """Return the user_id of the message currently being processed, if any."""
    return _user_id.get()


def current_session_id() -> str:
    return _session_id.get()


def current_platform() -> str:
    return _platform.get()


def current_chat_id() -> str:
    return _chat_id.get()