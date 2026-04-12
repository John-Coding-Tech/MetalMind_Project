"""
services/tavily_client.py

Tavily integration layer.

RULES (from tools/tools.md):
- ALWAYS use Tavily — NEVER hardcode supplier data
- NEVER skip Tavily search
- Return raw results for cleaning; do NOT process here
"""

import os
from typing import Any

from tavily import TavilyClient


def _get_client() -> TavilyClient:
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
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
            - title   (str)
            - url     (str)
            - content (str)
            - score   (float) — Tavily relevance score
    """
    client = _get_client()

    response = client.search(
        query=query,
        search_depth="advanced",
        max_results=max_results,
        include_answer=False,
    )

    results: list[dict] = response.get("results", [])
    return results


def search_india_suppliers(max_results: int = 10) -> list[dict[str, Any]]:
    """Search for ACP panel manufacturers in India."""
    return search_suppliers(
        "ACP aluminium composite panel manufacturers suppliers India price per sqm",
        max_results=max_results,
    )


def search_china_suppliers(max_results: int = 10) -> list[dict[str, Any]]:
    """Search for ACP panel manufacturers in China."""
    return search_suppliers(
        "ACP aluminium composite panel manufacturers suppliers China price per sqm",
        max_results=max_results,
    )
