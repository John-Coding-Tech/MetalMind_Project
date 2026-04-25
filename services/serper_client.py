"""
services/serper_client.py

Primary search client using Serper (Google Search API).

This is the MAIN search source. Tavily is fallback/enrichment only.

Public API:
    search(query, max_results=10) -> list[dict]

Returns results in the same schema as Tavily so cleaner.py works unchanged:
    {"title": str, "url": str, "content": str, "score": float, "source": "serper"}

Environment:
    SERPER_API_KEY — required
"""

import logging
import os
import time
from typing import Any

import requests

_log = logging.getLogger(__name__)

_API_URL = "https://google.serper.dev/search"
_TIMEOUT = 7   # per-call hard cap; multi_search runs 8 calls in parallel under a 20s budget


class SerperError(Exception):
    """Raised on any Serper failure."""


# ---------------------------------------------------------------------------
# TTL cache — 12h. Serper is paid per query; supplier-search results barely
# move within a day, so caching aggressively saves real money. Key is
# normalized (lowercased, whitespace-collapsed) so equivalent phrasings hit
# the same entry.
# ---------------------------------------------------------------------------

_CACHE_TTL = 12 * 60 * 60   # 12 hours
_cache: dict[str, tuple[float, list[dict]]] = {}


def _normalize_query_key(query: str, max_results: int) -> str:
    norm = " ".join((query or "").lower().split())
    return f"{norm}|{max_results}"


def _cache_get(key: str) -> list[dict] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return data


def _cache_set(key: str, data: list[dict]) -> None:
    _cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------

def search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """
    Search via Serper (Google Search). Returns results in the unified format
    that cleaner.py expects: {title, url, content, score, source}.
    """
    cache_key = _normalize_query_key(query, max_results)
    cached = _cache_get(cache_key)
    if cached is not None:
        _log.info("[serper] CACHE HIT  q='%s' (%d results)", query[:40], len(cached))
        return cached

    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        raise SerperError("SERPER_API_KEY is not set.")

    t0 = time.time()

    try:
        resp = requests.post(
            _API_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results},
            timeout=_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise SerperError(f"Serper request failed: {e}") from e

    if not resp.ok:
        raise SerperError(f"Serper HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    organic = data.get("organic", []) or []

    results: list[dict] = []
    for i, item in enumerate(organic[:max_results]):
        results.append({
            "title":   item.get("title", ""),
            "url":     item.get("link", ""),
            "content": item.get("snippet", ""),
            "score":   round(1.0 - (i / max(max_results, 1)), 3),
            "source":  "serper",
        })

    elapsed = time.time() - t0
    _cache_set(cache_key, results)
    _log.info("[serper] FETCHED  q='%s' -> %d results in %.1fs", query[:40], len(results), elapsed)

    return results
