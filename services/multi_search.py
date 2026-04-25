"""
services/multi_search.py

Multi-angle, multi-country supplier search built on top of serper_client.

Flow:
  1. Take a parsed query dict from engine.query_parser.parse_search_query().
  2. Build a set of (country, angle, query) plans using priority allocation
     under a max_calls budget (default 8, since Serper is paid per request).
  3. Run all plans in parallel via the existing serper_client.
  4. Merge results by URL, recording which angles matched each URL.
  5. Return a single list sorted by (angle_count desc, score desc).

The "angle_count" field is the trust signal we surface to the frontend: a URL
matched by 3 different angles (supplier + price + spec) is more trustworthy
than one matched only by a single keyword query.

Public API:
    multi_search_and_merge(parsed: dict, max_calls: int = 8,
                           per_query_results: int = 10) -> list[dict]
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.serper_client import search as serper_search, SerperError
from services import tavily_client

_log = logging.getLogger(__name__)


# Number of top-ranked URLs we deep-fetch via Tavily. ACP and most metal
# supplier sites show prices on the actual page but not in Google's snippet,
# so without this step the cleaner has only ~200 chars of text per result
# and price extraction fails for ~80% of suppliers.
_ENRICH_TOP_N = 3


# ---------------------------------------------------------------------------
# Angle priority — order matters for budget allocation.
# "supplier" is always added first because it is the only angle that reliably
# returns vendor pages; the rest enrich the merge with extra trust signals.
# ---------------------------------------------------------------------------

_ANGLE_PRIORITY: list[str] = ["supplier", "price", "spec"]
# Previously included "cert" as a 4th angle ("... ISO certified manufacturer
# in <country>"), but the return almost always consisted of marketing
# boilerplate rather than genuine certification signal — and cleaner.py's
# _CERT_KEYWORDS scan already detects certs from the scraped page text
# whether or not we specifically searched for them. Dropping the angle
# shaves ~25% off Serper spend per analysis with no measurable loss in
# supplier coverage.


# Map our internal category code to a human-readable noun phrase used inside
# the search query string.
_CATEGORY_DISPLAY: dict[str, str] = {
    "acp":             "ACP aluminium composite panel",
    "aluminum":        "aluminium sheet",
    "steel":           "carbon steel",
    "stainless_steel": "stainless steel",
    "copper":          "copper sheet",
    "brass":           "brass sheet",
    "zinc":            "zinc sheet",
    "titanium":        "titanium sheet",
    "tube":            "metal tube",
    "pipe":            "metal pipe",
    "unknown":         "metal",
}


# ---------------------------------------------------------------------------
# Query builders — one per angle
# ---------------------------------------------------------------------------

def _product_phrase(parsed: dict) -> str:
    """
    Build a noun phrase describing the product, combining material + variant
    + category. Used as the core of every angle query.
    """
    cat      = parsed.get("category") or "unknown"
    material = (parsed.get("material") or "").strip()
    variant  = (parsed.get("variant")  or "").strip()
    cat_name = _CATEGORY_DISPLAY.get(cat, "metal")

    parts: list[str] = []
    if material:
        parts.append(material)
    if variant and variant != "solid":
        # Convert "pvdf_coated" -> "PVDF coated", "wooden" -> "wooden"
        parts.append(variant.replace("_", " "))
    parts.append(cat_name)
    return " ".join(parts).strip()


def _country_clause(country: str) -> str:
    return f" in {country}" if country else ""


def _q_supplier(parsed: dict, country: str) -> str:
    return f"{_product_phrase(parsed)} manufacturer official site{_country_clause(country)}".strip()


def _q_price(parsed: dict, country: str) -> str:
    pr   = parsed.get("price_range") or {}
    unit = pr.get("unit") or _default_unit_phrase(parsed.get("category"))
    return f"{_product_phrase(parsed)} price per {unit}{_country_clause(country)}".strip()


def _q_cert(parsed: dict, country: str) -> str:
    return f"{_product_phrase(parsed)} ISO certified manufacturer{_country_clause(country)}".strip()


def _q_spec(parsed: dict, country: str) -> str:
    spec = (parsed.get("spec") or "").strip()
    if not spec:
        return ""
    return f"{_product_phrase(parsed)} {spec} specification{_country_clause(country)}".strip()


_BUILDERS = {
    "supplier": _q_supplier,
    "price":    _q_price,
    "cert":     _q_cert,
    "spec":     _q_spec,
}


def _default_unit_phrase(category: str | None) -> str:
    """Default per-unit phrase for the price angle when no unit was parsed."""
    if category == "acp":
        return "sqm"
    if category in ("aluminum", "steel", "stainless_steel",
                    "copper", "brass", "zinc", "titanium"):
        return "ton"
    if category in ("tube", "pipe"):
        return "meter"
    return "unit"


# ---------------------------------------------------------------------------
# Budget allocation
# ---------------------------------------------------------------------------

def _allocate_budget(
    parsed: dict,
    countries: list[str],
    max_calls: int,
) -> list[tuple[str, str, str]]:
    """
    Return a list of (country, angle, query) plans whose length is <= max_calls.

    Allocation strategy:
      Round 1 — `supplier` for each country (most important angle).
      Round 2 — cycle through `price`, `cert`, `spec` adding one (country, angle)
                plan at a time, in priority order.
      Skip — any builder that returns "" (e.g. `spec` with no spec text).
    """
    plans: list[tuple[str, str, str]] = []

    def _try_add(country: str, angle: str) -> bool:
        if len(plans) >= max_calls:
            return False
        q = _BUILDERS[angle](parsed, country)
        if not q:
            return False
        plans.append((country, angle, q))
        return True

    # Round 1: supplier for every country
    for c in countries:
        _try_add(c, "supplier")
        if len(plans) >= max_calls:
            return plans

    # Round 2+: cycle remaining angles
    for angle in _ANGLE_PRIORITY[1:]:
        for c in countries:
            _try_add(c, angle)
            if len(plans) >= max_calls:
                return plans

    return plans


# ---------------------------------------------------------------------------
# Parallel execution + merge
# ---------------------------------------------------------------------------

def _run_plans(
    plans: list[tuple[str, str, str]],
    per_query_results: int,
) -> list[tuple[str, list[dict]]]:
    """
    Execute every plan in parallel. Returns list of (angle, results) tuples.
    Failed plans return (angle, []).
    """
    if not plans:
        return []

    def _do(plan: tuple[str, str, str]) -> tuple[str, list[dict]]:
        country, angle, query = plan
        try:
            batch = serper_search(query, max_results=per_query_results)
            _log.info("[multi_search] %s/%s '%s' -> %d", country or "global",
                      angle, query[:60], len(batch))
            return (angle, batch)
        except SerperError as e:
            _log.warning("[multi_search] %s/%s failed: %s", country or "global", angle, e)
            return (angle, [])
        except Exception as e:  # noqa: BLE001 — never let one bad plan kill the run
            _log.error("[multi_search] %s/%s unexpected error: %s",
                       country or "global", angle, e)
            return (angle, [])

    workers = min(len(plans), 8)
    out: list[tuple[str, list[dict]]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_do, p) for p in plans]
        for fut in as_completed(futures):
            out.append(fut.result())
    return out


def _merge_by_url(angle_batches: list[tuple[str, list[dict]]]) -> list[dict]:
    """
    Merge all batches into a single list, deduping by URL. For each URL we
    track the SET of angles that matched it (`angles_matched`) so callers can
    weight multi-angle hits as more trustworthy.
    """
    by_url: dict[str, dict] = {}

    for angle, batch in angle_batches:
        for r in batch:
            url = (r.get("url") or "").strip()
            if not url:
                continue

            existing = by_url.get(url)
            if existing is None:
                # First time — clone the dict so we don't mutate the cache layer
                merged = dict(r)
                merged["angles_matched"] = {angle}
                by_url[url] = merged
                continue

            # Already seen — record this angle and keep the better score
            existing["angles_matched"].add(angle)
            if r.get("score", 0) > existing.get("score", 0):
                existing["score"] = r["score"]

    # Finalize — convert sets to sorted lists and add angle_count
    merged_list: list[dict] = []
    for r in by_url.values():
        angles = sorted(r["angles_matched"])
        r["angles_matched"] = angles
        r["angle_count"]    = len(angles)
        merged_list.append(r)

    # Sort: more angles first, then by raw score
    merged_list.sort(
        key=lambda r: (r["angle_count"], r.get("score", 0)),
        reverse=True,
    )
    return merged_list


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _enrich_top_n(results: list[dict], n: int) -> list[dict]:
    """
    Deep-fetch the top-N URLs via Tavily and replace their snippet-only
    `content` with the full page text. Lets the downstream cleaner extract
    prices that don't appear in Google's snippet (very common: ACP and most
    metal suppliers show pricing on the page itself, not in the meta tags).

    No-op when Tavily is not configured. Failures per-URL are silent — we
    keep the original snippet rather than dropping the result.
    """
    if not tavily_client.is_available() or n <= 0 or not results:
        return results

    head = results[:n]
    tail = results[n:]
    t0 = time.time()

    def _do(r: dict) -> dict:
        try:
            enriched = tavily_client.enrich_url(r.get("url", ""))
        except Exception as e:  # noqa: BLE001
            _log.debug("[multi_search] enrich failed for %s: %s", r.get("url"), e)
            return r
        if enriched and enriched.get("content"):
            r = dict(r)                # avoid mutating the cache layer
            r["content"]  = enriched["content"]
            r["enriched"] = True
        return r

    try:
        with ThreadPoolExecutor(max_workers=min(n, 3)) as ex:
            head = list(ex.map(_do, head))
        _log.info("[multi_search] enriched top-%d in %.1fs", n, time.time() - t0)
    except Exception as e:  # noqa: BLE001
        _log.warning("[multi_search] enrichment phase failed: %s", e)

    return head + tail


def multi_search_and_merge(
    parsed: dict,
    max_calls: int = 8,
    per_query_results: int = 10,
    enrich_top_n: int = _ENRICH_TOP_N,
) -> list[dict]:
    """
    Run a multi-angle, multi-country Serper search and return merged results.

    Args:
        parsed:            Output from engine.query_parser.parse_search_query().
        max_calls:         Hard cap on Serper API calls (default 8, paid API).
        per_query_results: Result count requested from Serper per call.
        enrich_top_n:      How many top-ranked URLs to deep-fetch via Tavily
                           after the merge. 0 disables enrichment.

    Returns:
        list[dict] — Each entry has the standard serper_client schema plus:
            "angles_matched": list[str]   — e.g. ["supplier", "price"]
            "angle_count":    int          — len(angles_matched)
            "enriched":       bool         — True when top-N Tavily fetch
                                             upgraded the snippet to full page
        Sorted by (angle_count desc, score desc).
    """
    countries: list[str] = parsed.get("countries") or [""]  # "" = global search

    plans = _allocate_budget(parsed, countries, max_calls)
    if not plans:
        _log.warning("[multi_search] no plans built (parsed=%s)", parsed)
        return []

    t0 = time.time()
    angle_batches = _run_plans(plans, per_query_results)
    merged = _merge_by_url(angle_batches)
    merged = _enrich_top_n(merged, enrich_top_n)
    elapsed = time.time() - t0

    _log.info(
        "[multi_search] %d plans -> %d unique URLs in %.1fs (countries=%s, angles=%s, enriched=%d)",
        len(plans), len(merged), elapsed,
        countries,
        sorted({a for a, _ in angle_batches}),
        sum(1 for r in merged if r.get("enriched")),
    )
    return merged
