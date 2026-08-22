from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import typer
import yaml

from aaagent.core.app import Application
from aaagent.core.logctx import ContextFilter
from aaagent.core.plugin import CLI_COMMAND_GROUP, PluginManager

app = typer.Typer(name="aaagent", help="A pluggable IM + LLM agent framework")

_AUTH_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+")

logger = logging.getLogger("aaagent.cli")


class _AuthScrubbingHandler(logging.StreamHandler):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return _AUTH_RE.sub(r"\1***", msg)


def _setup_logging(level: str = "INFO", quiet: bool = False) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(session_id)s/%(platform)s] %(name)s: %(message)s"
    )

    if quiet:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        handler = logging.FileHandler(log_dir / "aaagent.log", encoding="utf-8")
    else:
        handler = _AuthScrubbingHandler()

    handler.setFormatter(fmt)
    handler.addFilter(ContextFilter())

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def _run_application(application: Application) -> None:
    try:
        asyncio.run(application.run())
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger("aaagent").exception("Application crashed")
        raise SystemExit(1)


def _read_log_level(config_path: str) -> str:
    try:
        p = Path(config_path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("log_level", "INFO")
    except Exception:
        pass
    return "INFO"


def _read_config(config_path: str) -> dict:
    """Best-effort config load used by CLI commands that need it before
    the full Application is constructed (e.g. to discover the plugin
    set, surface a helpful error if config is missing, ...).

    Returns an empty dict if the file is missing or unreadable. The
    full Application does its own strict loading later.
    """
    try:
        p = Path(config_path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _load_cli_commands(config_path: str) -> None:
    """Discover and register top-level `aaagent <name>` subcommands from
    the `aaagent.cli_commands` entry-point group.

    Done eagerly at CLI import time so plugins can extend the surface
    simply by being installed (no separate registration step). Plugin
    registrars are called with `(typer_app, config_path)` and may add
    one or more subcommands; failures are logged but don't abort the
    CLI — the core built-ins still work.
    """
    cfg = _read_config(config_path)
    pm = PluginManager(cfg)
    # Only need the CLI-command registrars — skip provider/tool/adapter
    # validation that the full Application would do.
    pm._load_entry_points()
    for name, registrar in pm.get_cli_command_registrars().items():
        try:
            registrar(app, config_path)
        except Exception:  # noqa: BLE001
            logger.exception(
                "CLI command registrar '%s' (group %s) failed to register",
                name,
                CLI_COMMAND_GROUP,
            )


# Eagerly load plugin-supplied CLI commands at import time. This makes
# `aaagent web`, `aaagent foo`, etc. discoverable through Typer just
# like the built-in `run` / `chat`. The default config path is
# `config.yaml`; the `AAAGENT_CONFIG` env var (read by the CLI later)
# can override it. We pass the same default here so `--help` reflects
# the actual surface.
import os as _os  # noqa: E402

_load_cli_commands(_os.environ.get("AAAGENT_CONFIG", "config.yaml"))


@app.command()
def run(config: str = "config.yaml") -> None:
    """Start all enabled adapters."""
    _setup_logging(_read_log_level(config))
    application = Application(config_path=config)
    _run_application(application)


@app.command()
def chat(config: str = "config.yaml") -> None:
    """Start CLI chat mode for testing.

    Resolves the CLI adapter through the same PluginManager the rest of the
    application uses. If `aaagent-plugin-cliadapter` is not installed the
    command exits with an install hint.
    """
    _setup_logging(_read_log_level(config), quiet=True)
    application = Application(config_path=config, enabled_adapters=[])

    # Reuse the application's already-initialised plugin manager so we don't
    # have a second, parallel discovery path.
    cli_cls = application._plugins.get_adapter_class("cli")
    if cli_cls is None:
        logger.error(
            "CLI adapter plugin not installed. Run: pip install aaagent-plugin-cliadapter"
        )
        raise SystemExit(1)
    cli_adapter = cli_cls({}, application._bus)
    application.add_adapter(cli_adapter)
    _run_application(application)
