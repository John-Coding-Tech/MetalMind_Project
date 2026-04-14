"""
services/tavily_client.py

Tavily integration layer.

RULES (from tools/tools.md):
- ALWAYS use Tavily — NEVER hardcode supplier data
- NEVER skip Tavily search
- Return raw results for cleaning; do NOT process here

Errors are normalized into TavilyError so upstream endpoints can return
consistent HTTP status codes instead of bubbling random library exceptions.
"""

import os
from typing import Any

from tavily import TavilyClient


class TavilyError(Exception):
    """Raised for any Tavily failure (missing key, network, malformed response)."""


def _get_client() -> TavilyClient:
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        raise TavilyError(
            "TAVILY_API_KEY is not set. "
            "Add it to your .env file or environment variables."
        )
    return TavilyClient(api_key=api_key)


def search_suppliers(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """
    Search for ACP suppliers using Tavily.

    Args:
        query:       Search query string, e.g. "ACP manufacturers India"
        max_results: Maximum number of results to retrieve (default 10)

    Returns:
        List of raw result dicts from Tavily. Each dict contains:
            - title        (str)
            - url          (str)
            - content      (str)  — snippet merged with raw_content when available
            - score        (float) — Tavily relevance score

    Raises:
        TavilyError: key missing, network failure, non-200 response, or malformed body.
    """
    client = _get_client()

    try:
        response = client.search(
            query=query,
            search_depth="advanced",
            max_results=max_results,
            include_answer=False,
            include_raw_content=True,
        )
    except TavilyError:
        raise
    except Exception as e:
        raise TavilyError(f"Tavily search failed: {type(e).__name__}: {e}") from e

    if not isinstance(response, dict):
        raise TavilyError(f"Tavily returned non-dict response: {type(response).__name__}")

    results: list[dict] = response.get("results", []) or []

    # Merge raw_content into content so downstream modules get full page text
    # without needing to know about the extra field.
    for r in results:
        raw = r.get("raw_content") or ""
        if raw and raw not in r.get("content", ""):
            r["content"] = (r.get("content", "") + " " + raw).strip()

    return results


def search_india_suppliers(max_results: int = 10) -> list[dict[str, Any]]:
    """Search for ACP panel manufacturers in India."""
    return search_suppliers(
        "ACP aluminium composite panel manufacturer factory supplier India",
        max_results=max_results,
    )


def search_china_suppliers(max_results: int = 10) -> list[dict[str, Any]]:
    """Search for ACP panel manufacturers in China."""
    return search_suppliers(
        "ACP aluminium composite panel manufacturer factory supplier China",
        max_results=max_results,
    )
