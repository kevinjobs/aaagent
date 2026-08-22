from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aaagent_plugin_webscrape import WebScrapeToolsPlugin
from aaagent_plugin_webscrape import fetch_url as fetch_url_fn


HTML_PAGE = """<!doctype html>
<html>
  <head>
    <title>Sample Article</title>
    <meta charset="utf-8">
  </head>
  <body>
    <nav>menu one menu two</nav>
    <article>
      <h1>Sample Article</h1>
      <p>This is the main paragraph of the article. It contains several sentences
      that trafilatura should pick up as the core content of the page.</p>
      <p>A second paragraph adds more body text so extraction has enough to
      work with.</p>
      <a href="/about">About</a>
      <a href="https://other.example/foo">External</a>
    </article>
    <footer>footer junk that should be filtered</footer>
  </body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    body = HTML_PAGE.encode("utf-8")

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):  # silence
        return


def _make_handler(body: bytes) -> type[BaseHTTPRequestHandler]:
    class _Dyn(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return _Dyn


@pytest.fixture
def http_server():
    server = HTTPServer(("127.0.0.1", 0), _make_handler(HTML_PAGE.encode("utf-8")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_fetch_url_returns_markdown(http_server):
    out = await fetch_url_fn(
        {"url": http_server + "/", "format": "markdown"},
        timeout=5.0,
        max_bytes=1_000_000,
        user_agent="test-agent",
        follow_redirects=True,
        max_redirects=3,
    )
    assert "Sample Article" in out
    assert "main paragraph" in out
    assert "menu one" not in out


@pytest.mark.asyncio
async def test_fetch_url_text_format(http_server):
    out = await fetch_url_fn(
        {"url": http_server + "/", "format": "text"},
        timeout=5.0,
        max_bytes=1_000_000,
        user_agent="test-agent",
        follow_redirects=True,
        max_redirects=3,
    )
    assert "Sample Article" in out


@pytest.mark.asyncio
async def test_fetch_url_html_format_returns_raw_html(http_server):
    out = await fetch_url_fn(
        {"url": http_server + "/", "format": "html"},
        timeout=5.0,
        max_bytes=1_000_000,
        user_agent="test-agent",
        follow_redirects=True,
        max_redirects=3,
    )
    assert "<article>" in out
    assert "Sample Article" in out


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_scheme():
    out = await fetch_url_fn(
        {"url": "ftp://example.com/"},
        timeout=5.0,
        max_bytes=1_000_000,
        user_agent="test-agent",
        follow_redirects=True,
        max_redirects=3,
    )
    assert "Error" in out and "http://" in out


@pytest.mark.asyncio
async def test_fetch_url_requires_url():
    out = await fetch_url_fn(
        {"url": ""},
        timeout=5.0,
        max_bytes=1_000_000,
        user_agent="test-agent",
        follow_redirects=True,
        max_redirects=3,
    )
    assert "Error" in out and "url" in out


@pytest.mark.asyncio
async def test_fetch_url_truncates_output():
    html = "<html><body><p>" + "lorem ipsum " * 5000 + "</p></body></html>"
    body = html.encode("utf-8")
    server = HTTPServer(("127.0.0.1", 0), _make_handler(body))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        out = await fetch_url_fn(
            {"url": url, "max_chars": 200},
            timeout=5.0,
            max_bytes=10_000_000,
            user_agent="test-agent",
            follow_redirects=True,
            max_redirects=3,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "truncated" in out


@pytest.mark.asyncio
async def test_fetch_url_rejects_oversize_response():
    big = "x" * (2 * 1024 * 1024)
    html = f"<html><body><p>{big}</p></body></html>"
    body = html.encode("utf-8")
    server = HTTPServer(("127.0.0.1", 0), _make_handler(body))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        out = await fetch_url_fn(
            {"url": url},
            timeout=10.0,
            max_bytes=1024 * 1024,
            user_agent="test-agent",
            follow_redirects=True,
            max_redirects=3,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "Error" in out and "too large" in out


@pytest.mark.asyncio
async def test_webscrape_plugin_routes_via_registry(http_server):
    plugin = WebScrapeToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    plugin.register(
        registry,
        {"tools": {"webscrape": {"enabled": True, "timeout": 5, "max_bytes": 1_000_000}}},
    )
    out = await plugin._handler({"url": http_server + "/"})
    assert "Sample Article" in out
    assert "main paragraph" in out


@pytest.mark.asyncio
async def test_webscrape_plugin_disabled_skips_setup():
    plugin = WebScrapeToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    plugin.register(
        registry,
        {"tools": {"webscrape": {"enabled": False}}},
    )
    assert plugin._timeout == 20.0


@pytest.mark.asyncio
async def test_webscrape_plugin_handles_http_error():
    class _ErrorHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), _ErrorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        out = await fetch_url_fn(
            {"url": url},
            timeout=5.0,
            max_bytes=1_000_000,
            user_agent="test-agent",
            follow_redirects=True,
            max_redirects=3,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "Error" in out and "404" in out