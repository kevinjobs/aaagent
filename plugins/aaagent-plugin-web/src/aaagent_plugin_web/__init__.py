"""aaagent-plugin-web

Browser-based chat surface for aaagent, styled after DeepSeek
Harness. Provides:

* A `WebAdapter(IMAdapter)` that bridges the in-process EventBus to
  a set of WebSocket clients (see `adapter.py`).
* A FastAPI app (see `server.py`) that serves the SPA and the
  `/api/ws` endpoint.
* A `aaagent web` Typer subcommand (registered via the
  `aaagent.cli_commands` entry-point group) that boots the
  Application together with the web adapter and starts uvicorn.

Plugin wiring happens automatically: installing this package makes
`aaagent web` available on the CLI without any change to core.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("aaagent.web")


def register_cli_command(typer_app, config_path: str) -> None:
    """Entry-point target for `aaagent.cli_commands`.

    Registers a single top-level Typer subcommand `web`. The
    implementation defers most of the wiring to `_run_web` so this
    module-level function stays a thin shell and Typer's
    introspection sees a real command function (instead of a
    partially-applied callable).
    """
    import typer

    @typer_app.command()
    def web(
        config: str = typer.Option(
            "config.yaml",
            "--config",
            "-c",
            help="Path to aaagent config.yaml.",
        ),
        host: str = typer.Option(
            "127.0.0.1",
            "--host",
            help="Interface to bind the web server to.",
        ),
        port: int = typer.Option(
            8848,
            "--port",
            "-p",
            help="TCP port for the web server.",
        ),
        open_browser: bool = typer.Option(
            True,
            "--open/--no-open",
            help="Open the default browser once the server is up.",
        ),
    ) -> None:
        """Start aaagent with a browser-based chat UI."""
        _run_web(
            config_path=config,
            host=host,
            port=port,
            open_browser=open_browser,
        )


def _run_web(config_path: str, host: str, port: int, open_browser: bool) -> None:
    """Build the Application, wire the WebAdapter, start uvicorn.

    Flow:
      1. Read config (host/port/open_browser merged from CLI > YAML).
      2. Build Application with `enabled_adapters=[]` so no adapter
         is started automatically; we add the WebAdapter by hand
         afterwards, which gives us a stable instance to hand to
         the FastAPI server.
      3. Construct the FastAPI app (eagerly — any wiring error
         surfaces before uvicorn starts).
      4. Start uvicorn in a daemon thread.
      5. Open the browser when the health endpoint answers.
      6. Drive the bus via `application.run()`.
    """
    from pathlib import Path

    cfg = Path(config_path)
    if not cfg.exists():
        from typer import Exit

        logger.error(
            "config not found at %s. Run `aaagent web` from a directory containing %s, "
            "or pass --config <path>.",
            cfg.resolve(),
            config_path,
        )
        raise Exit(code=1)

    # Late import so this module is importable even if FastAPI /
    # uvicorn aren't installed (so users get a clearer error).
    from aaagent.core.app import Application
    from aaagent_plugin_web.adapter import WebAdapter
    from aaagent_plugin_web.server import build_app, serve

    # Read web-specific config to apply host/port defaults if the
    # user set them in config.yaml (CLI flags win).
    import yaml

    try:
        with open(cfg, encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        raw_cfg = {}
    web_cfg = ((raw_cfg.get("adapters") or {}).get("web") or {})
    if host == "127.0.0.1":
        host = str(web_cfg.get("host", host))
    if port == 8848 and "port" in web_cfg:
        port = int(web_cfg["port"])
    if "open_browser" in web_cfg:
        open_browser = bool(web_cfg["open_browser"])

    # Build the Application with no automatic adapters. We add the
    # WebAdapter manually below so the server has a stable reference
    # to it.
    application = Application(
        config_path=str(cfg),
        enabled_adapters=[],
    )
    web_adapter = WebAdapter(web_cfg, application._bus)
    application.add_adapter(web_adapter)

    # Build the FastAPI app now (eagerly, so any wiring errors
    # surface before uvicorn starts).
    dist_dir = _resolve_dist_dir()
    _ = build_app(web_adapter, dist_dir=dist_dir)

    # Start uvicorn in a background thread so the Application's main
    # loop can own its asyncio loop. The thread is a daemon so a
    # KeyboardInterrupt tears everything down together.
    import threading

    def _run_server() -> None:
        try:
            serve(web_adapter, host=host, port=port, dist_dir=dist_dir)
        except Exception:  # noqa: BLE001
            logger.exception("uvicorn thread crashed")

    server_thread = threading.Thread(
        target=_run_server,
        name="aaagent-web-uvicorn",
        daemon=True,
    )
    server_thread.start()

    if open_browser:
        _open_browser_when_ready(host, port)

    # Drive the bus. When this returns (Ctrl+C, exception, ...), the
    # daemon thread reaps on interpreter exit; we don't need to
    # explicitly stop uvicorn.
    import asyncio

    try:
        asyncio.run(application.run())
    except KeyboardInterrupt:
        logger.info("aaagent web: shutting down")
    finally:
        server_thread.join(timeout=2.0)


def _resolve_dist_dir():
    """Find the bundled SPA, even when running from a wheel.

    Resolution order:
    1. `aaagent_plugin_web/web/dist/` next to the package (wheel layout)
    2. `<source>/plugins/aaagent-plugin-web/web/dist/` (source layout)
    3. None — caller serves the fallback page
    """
    from pathlib import Path

    here = Path(__file__).resolve().parent
    candidates = [
        here / "web" / "dist",
        here.parent / "web" / "dist",
        here.parent.parent / "web" / "dist",
    ]
    for cand in candidates:
        if (cand / "index.html").exists():
            return cand
    return None


def _open_browser_when_ready(host: str, port: int) -> None:
    """Poll the health endpoint and open the default browser once it
    responds. Skipped on headless hosts (no DISPLAY / no GUI shell).
    """
    import threading
    import time
    import urllib.request
    import webbrowser

    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/api/health"

    def _poll() -> None:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=0.5) as r:
                    if r.status == 200:
                        page = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/"
                        try:
                            webbrowser.open(page)
                        except Exception:  # noqa: BLE001
                            logger.info("Open %s in your browser.", page)
                        return
            except Exception:
                time.sleep(0.2)
        logger.warning(
            "web server didn't come up within 10s; skipping browser open"
        )

    threading.Thread(target=_poll, daemon=True).start()


__all__ = ["register_cli_command", "WebAdapter"]
