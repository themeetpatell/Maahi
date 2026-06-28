"""Web tools — search + fetch, no API keys required.

Uses DuckDuckGo (via `ddgs` library) for search and `httpx` for fetching.

Both `httpx` and `ddgs` are imported lazily inside the functions so the rest
of Maahi can load even if these optional deps haven't been installed yet.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def web_search(query: str, max_results: int = 6) -> dict[str, object]:
    """Search the web. Returns a list of {title, url, snippet}."""
    try:
        # Prefer the maintained fork name `ddgs`; fall back to `duckduckgo_search`.
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore
    except ImportError:
        return {"ok": False, "error": "Install `ddgs` (pip install ddgs)."}

    results: list[dict[str, str]] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href") or r.get("url", ""),
                    "snippet": r.get("body", ""),
                })
    except Exception as e:  # noqa: BLE001 — third-party can raise anything
        return {"ok": False, "error": f"Search failed: {e}"}
    return {"ok": True, "query": query, "results": results, "count": len(results)}


def web_fetch(url: str, max_chars: int = 4000) -> dict[str, object]:
    """Fetch a URL and return the visible text content (HTML stripped)."""
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http(s)://"}
    try:
        import httpx  # lazy
    except ImportError:
        return {"ok": False, "error": "Install httpx (pip install httpx)."}
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "Maahi/0.1 (Mac)"}) as c:
            r = c.get(url)
            r.raise_for_status()
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Fetch failed: {e}"}
    text = _html_to_text(r.text)
    truncated = len(text) > max_chars
    return {
        "ok": True,
        "url": str(r.url),
        "status": r.status_code,
        "text": text[:max_chars],
        "truncated": truncated,
    }


# ============================================================
#  Tiny HTML-to-text. Not perfect — good enough for voice answers.
# ============================================================

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    s = _SCRIPT_STYLE.sub(" ", html)
    s = _TAG.sub(" ", s)
    # decode common entities
    for ent, ch in (("&amp;", "&"), ("&nbsp;", " "), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(ent, ch)
    s = _WS.sub(" ", s).strip()
    return s
