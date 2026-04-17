"""
services/search.py

Multi-source supplier search orchestrator.

Architecture:
  1. Serper (primary) — targeted queries for official manufacturer sites
  2. Tavily (fallback only) — if Serper returns < MIN_RESULTS
  3. Deduplication — by domain, with domain-quality scoring
  4. Strict filtering — blogs, directories, marketplaces all dropped
  5. Enrichment — Tavily deep content for top-3 only (max 3 calls)

Goal: reduce noise, improve signal quality, stabilize scoring.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from services.serper_client import search as serper_search, SerperError
from services import tavily_client
from services.tavily_client import TavilyError

_log = logging.getLogger(__name__)

_MIN_RESULTS = 5
_MAX_ENRICH  = 3


# ---------------------------------------------------------------------------
# Queries — intent-focused for official manufacturer sites, NOT "best" lists
# ---------------------------------------------------------------------------

_QUERIES = {
    "India": [
        "ACP aluminium composite panel manufacturer official site India",
        "ACP panel factory company India",
    ],
    "China": [
        "ACP aluminium composite panel manufacturer official site China",
        "ACP panel factory company China",
    ],
}


# ---------------------------------------------------------------------------
# Domain classification
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return url.lower()


# Marketplaces — these list thousands of "suppliers" but are NOT official sites
_MARKETPLACE_DOMAINS = {
    "made-in-china.com", "alibaba.com", "aliexpress.com",
    "globalsources.com", "indiamart.com", "tradeindia.com",
    "ec21.com", "tradekey.com", "exportersindia.com",
    "dhgate.com", "1688.com",
}

# Content/social — never suppliers
_JUNK_DOMAINS = {
    "wikipedia.org", "reddit.com", "quora.com", "medium.com",
    "youtube.com", "facebook.com", "twitter.com", "linkedin.com",
    "pinterest.com", "amazon.com", "ebay.com",
    "blogger.com", "wordpress.com",
    "archdaily.com", "dezeen.com",
    "constructionweek.com", "buildingmaterials.com",
}

# All domains to reject
_BLOCKED_DOMAINS = _MARKETPLACE_DOMAINS | _JUNK_DOMAINS


def _domain_quality_score(domain: str) -> float:
    """
    Score a domain on how likely it is an official supplier site.

    +0.3  official-looking domain (short, branded)
    +0.0  neutral
    -0.5  marketplace (made-in-china, alibaba, etc.)
    -1.0  junk (reddit, wikipedia, etc.)
    """
    if any(junk in domain for junk in _JUNK_DOMAINS):
        return -1.0
    if any(mp in domain for mp in _MARKETPLACE_DOMAINS):
        return -0.5
    parts = domain.split(".")
    name = parts[0] if parts else domain
    if len(name) <= 20 and not any(c.isdigit() for c in name):
        return 0.3
    return 0.0


# ---------------------------------------------------------------------------
# Strict filtering
# ---------------------------------------------------------------------------

_TITLE_DROP_PATTERNS = [
    re.compile(r"\btop\s+\d+\b", re.I),
    re.compile(r"\bbest\s+\d+\b", re.I),
    re.compile(r"\d+\s+best\b", re.I),
    re.compile(r"\bbest\b.{0,20}\b(suppliers?|manufacturers?|companies)\b", re.I),
    re.compile(r"\btop\b.{0,20}\b(suppliers?|manufacturers?|companies)\b", re.I),
    re.compile(r"\blist\s+of\b", re.I),
    re.compile(r"\bguide\b", re.I),
    re.compile(r"\bhow\s+to\b", re.I),
    re.compile(r"\bwhat\s+is\b", re.I),
    re.compile(r"\bvs\b", re.I),
    re.compile(r"\bcomparison\b", re.I),
    re.compile(r"\bprice\s+list\b", re.I),
    re.compile(r"\bmanufacturers\s+in\b", re.I),
    re.compile(r"\bsuppliers\s+in\b", re.I),
    re.compile(r"\bcompanies\s+in\b", re.I),
    re.compile(r"\bbrands\s+(&|and)\b", re.I),
    re.compile(r"\breview\b", re.I),
    re.compile(r"\bblog\b", re.I),
    re.compile(r"\bnews\b", re.I),
    re.compile(r"\barticle\b", re.I),
]


def _is_usable_result(r: dict) -> bool:
    """
    Strict pre-filter. Only keeps results that look like actual supplier pages.

    Rejects:
      - Blocked domains (marketplaces, social, content sites)
      - Titles matching blog/directory/article patterns
      - URLs with non-product path segments
    """
    title  = r.get("title", "")
    url    = r.get("url", "")
    domain = _extract_domain(url)

    if any(blocked in domain for blocked in _BLOCKED_DOMAINS):
        return False

    if any(p.search(title) for p in _TITLE_DROP_PATTERNS):
        return False

    path = urlparse(url).path.lower()
    if any(seg in path for seg in ("/blog", "/news", "/article", "/wiki", "/forum")):
        return False

    return True


# ---------------------------------------------------------------------------
# Domain-based deduplication with quality scoring
# ---------------------------------------------------------------------------

def _dedupe_by_domain(results: list[dict]) -> list[dict]:
    """
    Keep one entry per domain. Ties broken by:
      1. Domain quality score (official > marketplace)
      2. Search relevance score
    """
    seen: dict[str, dict] = {}
    for r in results:
        domain = _extract_domain(r.get("url", ""))
        if not domain:
            continue

        dq = _domain_quality_score(domain)
        composite = r.get("score", 0) + dq

        existing = seen.get(domain)
        if existing is None:
            r["_composite"] = composite
            seen[domain] = r
        elif composite > existing.get("_composite", 0):
            r["_composite"] = composite
            seen[domain] = r

    deduped = sorted(seen.values(), key=lambda r: r.get("_composite", 0), reverse=True)

    for r in deduped:
        r.pop("_composite", None)

    _log.info("[search] dedup: %d -> %d unique domains", len(results), len(deduped))
    return deduped


# ---------------------------------------------------------------------------
# Enrichment — Tavily deep content for top-N ONLY (max 3 calls)
# ---------------------------------------------------------------------------

def _enrich_top_n(results: list[dict], n: int = _MAX_ENRICH) -> list[dict]:
    if not tavily_client.is_available():
        _log.info("[search] Tavily unavailable, skipping enrichment")
        return results

    to_enrich = results[:n]
    t0 = time.time()

    def _do_enrich(r: dict) -> dict:
        enriched = tavily_client.enrich_url(r.get("url", ""))
        if enriched and enriched.get("content"):
            r = dict(r)
            r["content"] = enriched["content"]
            r["enriched"] = True
        return r

    try:
        with ThreadPoolExecutor(max_workers=min(n, 3)) as ex:
            enriched = list(ex.map(_do_enrich, to_enrich))
        results = enriched + results[n:]
        _log.info("[search] enriched top-%d in %.1fs", n, time.time() - t0)
    except Exception as e:
        _log.warning("[search] enrichment failed: %s", e)

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_suppliers(country: str, max_results: int = 10) -> list[dict]:
    """
    Multi-source supplier search for a single country.

    Pipeline:
      1. Serper with intent-focused queries (official sites, not "best" lists)
      2. Tavily fallback only if Serper < 5
      3. Strict filtering (marketplaces, blogs, directories all dropped)
      4. Domain dedup with quality scoring (official domains ranked higher)
      5. Enrich top-3 via Tavily (deep content for scoring, max 3 calls)
    """
    queries = _QUERIES.get(country, [f"ACP aluminium composite panel manufacturer {country}"])
    t0 = time.time()

    # --- Step 1: Primary search (Serper) — run both queries, merge ---
    results: list[dict] = []
    for q in queries:
        try:
            batch = serper_search(q, max_results=max_results)
            results.extend(batch)
            _log.info("[search] serper q='%s' -> %d results", q[:50], len(batch))
        except SerperError as e:
            _log.error("[search] Serper failed: %s", e)

    # --- Step 2: Conditional Tavily fallback ---
    if len(results) < _MIN_RESULTS:
        _log.info("[search] only %d results, trying Tavily fallback for %s", len(results), country)
        try:
            fallback = tavily_client.search_fallback(queries[0], max_results=max_results)
            results.extend(fallback)
        except (TavilyError, Exception) as e:
            _log.warning("[search] Tavily fallback failed: %s", e)

    if not results:
        _log.warning("[search] no results from any source for %s", country)
        return []

    # --- Step 3: Strict filtering BEFORE dedup (drop noise early) ---
    before = len(results)
    results = [r for r in results if _is_usable_result(r)]
    _log.info("[search] filter: %d -> %d (dropped %d)", before, len(results), before - len(results))

    # --- Step 4: Deduplicate by domain with quality scoring ---
    results = _dedupe_by_domain(results)

    # --- Step 5: Enrich top-3 with Tavily deep content ---
    results = _enrich_top_n(results, n=_MAX_ENRICH)

    _log.info("[search] %s pipeline: %.1fs -> %d final results", country, time.time() - t0, len(results))
    return results
