"""Web fetch / scrape tool plugin for aaagent.

Uses httpx to retrieve a URL and trafilatura to extract the main content as
clean Markdown (or plain text / HTML). Designed for the common case of static
HTML pages; JS-heavy sites will fall back to title + first paragraph + link list.

Config example (config.yaml):

    tools:
      webscrape:
        enabled: true
        timeout: 20
        max_bytes: 5000000
        user_agent: "aaagent/0.3 (+https://example.invalid)"
        follow_redirects: true
        max_redirects: 5
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any

import httpx
import trafilatura
from aaagent.core.plugin import ToolPlugin

logger = logging.getLogger("aaagent.tools.webscrape")


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_LINK_RE = re.compile(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data


def _html_title(html: str) -> str:
    p = _TitleParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        return ""
    return p.title.strip()


def _strip_tags(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = _SPACE_RE.sub(" ", text)
    return text.strip()


def _extract_links(html: str, limit: int = 20) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(html):
        href = match.group(1).strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(href)
        if len(out) >= limit:
            break
    return out


def _format_fallback(url: str, html: str) -> str:
    title = _html_title(html) or "(no title)"
    body = _strip_tags(html)
    snippet = body[:600].strip()
    links = _extract_links(html)
    parts = [f"[url] {url}", f"[title] {title}", f"[snippet] {snippet}"]
    if links:
        parts.append("[links]\n" + "\n".join(links))
    return "\n\n".join(parts)


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated, {len(text)} total chars)"


async def fetch_url(
    args: dict[str, Any],
    *,
    timeout: float,
    max_bytes: int,
    user_agent: str,
    follow_redirects: bool,
    max_redirects: int,
) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return "Error: 'url' is required"
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Error: url must start with http:// or https://"

    fmt = (args.get("format") or "markdown").lower()
    if fmt not in {"markdown", "text", "html"}:
        return f"Error: invalid format '{fmt}' (allowed: markdown, text, html)"

    try:
        max_chars = int(args.get("max_chars", 20000))
    except (TypeError, ValueError):
        max_chars = 20000
    max_chars = max(0, min(max_chars, 200000))

    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    timeout_obj = httpx.Timeout(timeout, connect=min(timeout, 10.0))

    async with httpx.AsyncClient(
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        timeout=timeout_obj,
        headers=headers,
    ) as client:
        try:
            resp = await client.get(url)
        except httpx.TimeoutException:
            return f"Error: fetch timed out after {timeout}s"
        except httpx.TooManyRedirects:
            return "Error: too many redirects"
        except httpx.HTTPError as e:
            return f"Error: fetch failed ({e})"

    if resp.status_code >= 400:
        return f"Error: HTTP {resp.status_code} for {url}"

    content_length = resp.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        return (
            f"Error: response too large ({content_length} bytes > {max_bytes} max)"
        )

    raw = resp.content
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]

    html = raw.decode(resp.encoding or "utf-8", errors="replace")

    if fmt == "html":
        return _truncate(html, max_chars)

    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        include_links=False,
        output_format="markdown" if fmt == "markdown" else "txt",
        with_metadata=False,
    )

    if not extracted:
        logger.info("trafilatura returned empty for %s, falling back", url)
        return _truncate(_format_fallback(url, html), max_chars)

    return _truncate(extracted, max_chars)


class WebScrapeToolsPlugin(ToolPlugin):
    name = "webscrape"

    def __init__(self) -> None:
        self._timeout = 20.0
        self._max_bytes = 5_000_000
        self._user_agent = "aaagent/0.3 (+https://example.invalid)"
        self._follow_redirects = True
        self._max_redirects = 5

    def register(self, registry: Any, config: dict[str, Any]) -> None:
        cfg = (config.get("tools") or {}).get("webscrape") or {}
        if not cfg.get("enabled", True):
            return

        self._timeout = float(cfg.get("timeout", 20))
        self._max_bytes = int(cfg.get("max_bytes", 5_000_000))
        self._user_agent = str(
            cfg.get("user_agent") or "aaagent/0.3 (+https://example.invalid)"
        )
        self._follow_redirects = bool(cfg.get("follow_redirects", True))
        self._max_redirects = int(cfg.get("max_redirects", 5))

        registry.register(
            name="fetch_url",
            description=(
                "Fetch a URL and return its main content as clean Markdown (default), "
                "plain text, or HTML. Use this after web_search to read a specific page. "
                "JS-heavy pages may fall back to title + first paragraph + link list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http:// or https:// URL to fetch.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "text", "html"],
                        "description": "Output format (default: markdown).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 20000, max 200000).",
                    },
                },
                "required": ["url"],
            },
            handler=self._handler,
        )
        logger.info(
            "webscrape plugin registered (timeout=%s, max_bytes=%s)",
            self._timeout,
            self._max_bytes,
        )

    async def establish(self, registry: Any, config: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None

    async def _handler(self, args: dict[str, Any]) -> str:
        return await fetch_url(
            args,
            timeout=self._timeout,
            max_bytes=self._max_bytes,
            user_agent=self._user_agent,
            follow_redirects=self._follow_redirects,
            max_redirects=self._max_redirects,
        )


__all__ = ["WebScrapeToolsPlugin", "fetch_url"]