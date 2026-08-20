from __future__ import annotations

import asyncio
import logging
import re

import typer

from aaagent.adapters.cli_adapter import CliAdapter
from aaagent.core.app import Application

app = typer.Typer(name="aaagent", help="A pluggable IM + LLM agent framework")

_AUTH_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+")


class _AuthScrubbingHandler(logging.StreamHandler):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return _AUTH_RE.sub(r"\1***", msg)


def _setup_logging(level: str = "INFO") -> None:
    handler = _AuthScrubbingHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _run_application(application: Application) -> None:
    try:
        asyncio.run(application.run())
    except KeyboardInterrupt:
        pass


@app.command()
def _read_log_level(config_path: str) -> str:
    try:
        import yaml
        from pathlib import Path
        p = Path(config_path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("log_level", "INFO")
    except Exception:
        pass
    return "INFO"


@app.command()
def run(config: str = "config.yaml") -> None:
    """Start all enabled adapters."""
    _setup_logging(_read_log_level(config))
    application = Application(config_path=config)
    _run_application(application)


@app.command()
def chat(config: str = "config.yaml") -> None:
    """Start CLI chat mode for testing."""
    _setup_logging(_read_log_level(config))
    application = Application(config_path=config, enabled_adapters=[])

    cli_adapter = CliAdapter({}, application._bus)
    application.add_adapter(cli_adapter)
    _run_application(application)