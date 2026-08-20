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


@app.command()
def run(config: str = "config.yaml") -> None:
    """Start all enabled adapters."""
    application = Application(config_path=config)
    _setup_logging(application._config.get("log_level", "INFO"))

    async def _run() -> None:
        try:
            await application.run()
        except KeyboardInterrupt:
            await application.stop()

    asyncio.run(_run())


@app.command()
def chat(config: str = "config.yaml") -> None:
    """Start CLI chat mode for testing."""
    application = Application(config_path=config)
    _setup_logging(application._config.get("log_level", "INFO"))

    cli_adapter = CliAdapter({}, application._bus)
    application.add_adapter(cli_adapter)

    async def _run() -> None:
        try:
            await application.run()
        except KeyboardInterrupt:
            await application.stop()

    asyncio.run(_run())
