"""
services/tavily_client.py

Tavily — ENRICHMENT ONLY (not primary search).

Roles:
  1. Fallback search — called only when Serper returns < 5 results
  2. URL enrichment — deep content extraction for top suppliers (max 3 calls)

Tavily is NEVER the primary search source. Serper handles that.
"""

import logging
import os
import time
from typing import Any

_log = logging.getLogger(__name__)


class TavilyError(Exception):
    """Raised for any Tavily failure."""


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        raise TavilyError("TAVILY_API_KEY is not set.")
    from tavily import TavilyClient
    _client = TavilyClient(api_key=api_key)
    return _client


def is_available() -> bool:
    """Check if Tavily API key is configured (does not verify the key works)."""
    return bool(os.environ.get("TAVILY_API_KEY", ""))


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------

_CACHE_TTL = 300
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return data


def _cache_set(key: str, data) -> None:
    _cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# Fallback search — only called when Serper gives < 5 results
# ---------------------------------------------------------------------------

def search_fallback(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """
    Fallback search via Tavily. Returns results in the same schema as
    Serper so the orchestrator can merge them transparently.
    """
    cache_key = f"search|{query}|{max_results}"
    cached = _cache_get(cache_key)
    if cached is not None:
        _log.info("[tavily] CACHE HIT fallback q='%s'", query[:40])
        return cached

    client = _get_client()
    t0 = time.time()

    try:
        response = client.search(
            query=query,
            search_depth="basic",
            max_results=max_results,
            include_answer=False,
            include_raw_content=False,
        )
    except Exception as e:
        raise TavilyError(f"Tavily fallback search failed: {e}") from e

    if not isinstance(response, dict):
        raise TavilyError("Tavily returned non-dict response")

    raw_results = response.get("results", []) or []
    results: list[dict] = []
    for r in raw_results:
        results.append({
            "title":   r.get("title", ""),
            "url":     r.get("url", ""),
            "content": r.get("content", ""),
            "score":   float(r.get("score", 0)),
            "source":  "tavily",
        })

    _cache_set(cache_key, results)
    _log.info("[tavily] FALLBACK  q='%s' -> %d results in %.1fs", query[:40], len(results), time.time() - t0)
    return results


# ---------------------------------------------------------------------------
# Enrichment — deep content for a single URL (max 3 per analysis)
# ---------------------------------------------------------------------------

def enrich_url(url: str) -> dict | None:
    """
    Fetch deep content for a single supplier URL via Tavily extract.

    Returns {"content": str, "raw_content": str} or None on failure.
    Used to enrich the top-3 suppliers with full page content for better
    risk scoring and AI insight prompts.
    """
    cache_key = f"enrich|{url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        _log.info("[tavily] CACHE HIT enrich url='%s'", url[:50])
        return cached

    client = _get_client()
    t0 = time.time()

    try:
        response = client.extract(urls=[url])
    except Exception as e:
        _log.warning("[tavily] enrich failed for %s: %s", url[:50], e)
        return None

    results = (response or {}).get("results", []) or []
    if not results:
        return None

    r = results[0]
    raw_content = r.get("raw_content", "")
    content     = raw_content or r.get("text", "")

    if not content:
        return None

    enriched = {"content": content, "raw_content": raw_content}
    _cache_set(cache_key, enriched)
    _log.info("[tavily] ENRICHED  url='%s' in %.1fs (%d chars)", url[:50], time.time() - t0, len(content))
    return enriched
