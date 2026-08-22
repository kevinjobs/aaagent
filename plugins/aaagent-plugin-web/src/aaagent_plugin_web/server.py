"""FastAPI app factory + uvicorn bootstrap for the web adapter.

`build_app(adapter, dist_dir)` returns a FastAPI app that:

* Serves the bundled SPA from `dist_dir` if it exists.
* Falls back to a "frontend not built" landing page otherwise, so
  the plugin still works after a `pip install` even without the
  npm-built artifacts.
* Exposes `/api/ws` for the browser to talk to the agent.

`serve(adapter, host, port, dist_dir)` starts uvicorn in the same
process as the adapter and blocks until interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from aaagent_plugin_web.adapter import WebAdapter

logger = logging.getLogger("aaagent.web")


_FALLBACK_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>aaagent web</title>
    <style>
      body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
              "PingFang SC", "Microsoft YaHei", sans-serif;
              background: #f6f7f8; color: #0f1115;
              display: grid; place-items: center; min-height: 100vh; margin: 0; }}
      main {{ max-width: 560px; padding: 32px; background: #fff;
              border: 1px solid rgb(0 0 0 / 10%); border-radius: 12px; }}
      h1 {{ font-size: 18px; margin: 0 0 8px; }}
      code {{ background: #f0f1f2; padding: 2px 6px; border-radius: 4px;
              font-size: 13px; }}
      ol {{ margin: 12px 0 0; padding-left: 20px; line-height: 1.7; }}
    </style>
  </head>
  <body>
    <main>
      <h1>Frontend not built</h1>
      <p>The Python side of <code>aaagent-plugin-web</code> is running,
         but the browser assets haven't been generated yet.</p>
      <ol>
        <li>From the plugin source root:<br>
            <code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code></li>
        <li>Reload this page.</li>
      </ol>
      <p>The plugin works without the frontend build — the WebSocket
         endpoint at <code>/api/ws</code> is live and can be driven
         from <code>wscat</code> or any client.</p>
    </main>
  </body>
</html>
"""


def _find_dist_dir() -> Path | None:
    """Locate the bundled SPA. Looks next to this module first
    (installed wheel layout: `aaagent_plugin_web/web/dist/`), then
    falls back to the source-tree layout (`plugins/aaagent-plugin-web/
    web/dist/` relative to the wheel install)."""
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


def build_app(adapter: "WebAdapter", dist_dir: Path | None = None) -> FastAPI:
    """Construct the FastAPI app wired to the given adapter.

    The adapter owns the EventBus subscription; the server only
    handles the WebSocket ↔ adapter bridge and static asset serving.
    """
    app = FastAPI(
        title="aaagent web",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/api/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "platform": "web",
                "connections": len(adapter._pushers),
            }
        )

    @app.websocket("/api/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        queue = await adapter.register_pusher()

        async def pump_outbound() -> None:
            try:
                while True:
                    frame = await queue.get()
                    await ws.send_text(frame)
            except (WebSocketDisconnect, asyncio.CancelledError):
                return

        outbound_task = asyncio.create_task(pump_outbound())
        try:
            while True:
                text = await ws.receive_text()
                try:
                    import json

                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("web: malformed JSON frame; closing socket")
                    await ws.close(code=1003)
                    return
                # Heartbeat shortcut — keep the round-trip cheap. The
                # reply goes synchronously so keep-alive works even when
                # another LLM call is in flight on this same socket.
                if isinstance(data, dict) and data.get("type") == "ping":
                    await ws.send_text('{"type":"pong"}')
                    continue
                # Dispatch the bus event as a fire-and-forget task. This
                # keeps the WS receive loop responsive: a slow LLM call
                # on one frame cannot block the next ping/heartbeat or
                # a new user message from the same tab.
                # A task-level crash is caught by our default
                # unhandled-exception handler (bus._safe_call) so we
                # don't need to await here.
                asyncio.create_task(adapter.handle_inbound(data))
                continue
        except WebSocketDisconnect:
            return
        finally:
            outbound_task.cancel()
            try:
                await outbound_task
            except asyncio.CancelledError:
                pass
            await adapter.unregister_pusher(queue)

    # Static assets. If the SPA hasn't been built, serve the fallback
    # page on `/` so users see a clear "build me" message instead of
    # a 404. Sub-paths (`/assets/...`) just 404 — there's nothing to
    # serve until the build runs.
    dist_dir = dist_dir if dist_dir is not None else _find_dist_dir()
    if dist_dir is not None and (dist_dir / "index.html").exists():
        # FastAPI's StaticFiles with html=True serves index.html for
        # directory requests. We mount it at "/" so SPA routes work.
        app.mount(
            "/",
            StaticFiles(directory=str(dist_dir), html=True),
            name="spa",
        )
    else:
        @app.get("/", include_in_schema=False)
        async def _index() -> HTMLResponse:
            return HTMLResponse(_FALLBACK_HTML)

        # Catch-all 404 so the SPA doesn't get raw JSON 404s while
        # the build is missing.
        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_fallback(full_path: str) -> HTMLResponse:
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return HTMLResponse(_FALLBACK_HTML)

    return app


def serve(
    adapter: "WebAdapter",
    host: str = "127.0.0.1",
    port: int = 8848,
    log_level: str = "info",
    dist_dir: Path | None = None,
) -> None:
    """Start uvicorn in the foreground. Blocks until interrupted.

    The plugin's CLI command is expected to spawn this in a daemon
    thread so that `Application.run()` (which holds the asyncio loop)
    and uvicorn (which wants its own) can coexist.
    """
    app = build_app(adapter, dist_dir=dist_dir)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        # Don't open the file watcher — uvicorn's auto-reload would
        # duplicate plugins / configs.
        reload=False,
        # Match the asyncio policy the rest of aaagent uses. On
        # Windows this avoids "Event loop is closed" when the CLI
        # runner tears down its loop after `serve()` returns.
        loop="asyncio",
        access_log=os.environ.get("AAAGENT_WEB_ACCESS_LOG", "1") == "1",
    )
    server = uvicorn.Server(config)
    logger.info("aaagent web serving on http://%s:%d", host, port)
    server.run()


__all__ = ["build_app", "serve", "_find_dist_dir", "_FALLBACK_HTML"]
