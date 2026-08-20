from __future__ import annotations

import asyncio
import logging

import typer

from aaagent.adapters.cli_adapter import CliAdapter
from aaagent.core.app import Application

app = typer.Typer(name="aaagent", help="A pluggable IM + LLM agent framework")


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _run_application(application: Application) -> None:
    try:
        asyncio.run(application.run())
    except KeyboardInterrupt:
        pass


@app.command()
def run(config: str = "config.yaml") -> None:
    """Start all enabled adapters."""
    application = Application(config_path=config)
    _setup_logging(application._config.get("log_level", "INFO"))
    _run_application(application)


@app.command()
def chat(config: str = "config.yaml") -> None:
    """Start CLI chat mode for testing."""
    application = Application(config_path=config)
    _setup_logging(application._config.get("log_level", "INFO"))

    cli_adapter = CliAdapter({}, application._bus)
    application.add_adapter(cli_adapter)
    _run_application(application)
