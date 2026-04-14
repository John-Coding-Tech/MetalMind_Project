"""
modules/cleaner.py

Data cleaning module.

Responsibilities (from system/workflow.md Step 3):
- Extract supplier name, country, description, and estimated price
  from raw Tavily results.
- Convert price ranges (e.g. "14–18 USD/sqm") into a single mid-point value.
- Discard results that are clearly not ACP suppliers.
- Return a list of clean, structured SupplierRecord dicts.

NEVER pass raw Tavily output downstream — always clean first.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from modules import currency as _cur


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SupplierRecord:
    name:            str
    country:         str          # "India" | "China" | "Unknown"
    url:             str
    description:     str
    price_raw:       str          # original price text found (for display)
    price_est:       Optional[float]   # estimated USD/sqm (None if not found)
    relevance_score: float        # Tavily relevance score (0–1)
    raw_content:     str          # kept for risk scoring


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Currency prefix — non-capturing, covers $, ₹, ¥, €, Rs, USD, INR, CNY, RMB, EUR
_CUR_PFX     = r"(?:₹|¥|€|\$|a\$|rs\.?\s*|usd\s*|inr\s*|cny\s*|rmb\s*|eur\s*|aud\s*)?"
# Capturing variant — used in range form so we can validate both ends
# carry the same currency (or no currency).
_CUR_PFX_CAP = r"(₹|¥|€|\$|a\$|rs\.?\s*|usd\s*|inr\s*|cny\s*|rmb\s*|eur\s*|aud\s*)?"

_PRICE_PATTERN = re.compile(
    # Range: "₹500–800" | "$14–$18" | "14 to 18 USD"
    # Both ends capture their currency prefix; caller rejects mismatches.
    _CUR_PFX_CAP + r"(\d+(?:\.\d+)?)\s*[-–—to]+\s*" + _CUR_PFX_CAP + r"(\d+(?:\.\d+)?)"
    r"|"
    # Single: "₹500/sqm" | "$15 per sqm" | "15 USD/sqm"
    + _CUR_PFX + r"(\d+(?:\.\d+)?)\s*(?:usd|inr|cny|rmb|eur|aud|\/sqm|per\s*sqm|per\s*m2)?",
    re.IGNORECASE,
)

_INDIA_KEYWORDS  = ["india", "indian", "gujarat", "mumbai", "delhi", "chennai",
                     "rajkot", "ahmedabad", "pune", "hyderabad"]
_CHINA_KEYWORDS  = ["china", "chinese", "guangzhou", "shanghai", "beijing",
                     "shenzhen", "zhejiang", "fujian", "jiangsu"]
_ACP_KEYWORDS    = ["acp", "aluminium composite", "aluminum composite",
                     "alucobond", "alubond", "aludecor", "alstrong", "cladding"]

# Titles / URLs that indicate articles, guides, or price-comparison pages —
# NOT actual supplier websites. Checked against the page title only.
_NON_SUPPLIER_TITLE_PATTERNS = [
    "price & specification", "price and specification",
    "price list", "price guide", "pricing guide",
    "what is acp", "what is aluminium composite", "what is aluminum composite",
    "how to", "guide to", "introduction to",
    "everything you need to know",
    "vs ", " vs.", " comparison",
    "wikipedia", "wikimedia",
    "news:", "press release",
    "specifications and price", "specification & price",
]

# URL domains that are pure content/news/wiki sites, never suppliers
_NON_SUPPLIER_DOMAINS = [
    "wikipedia.org", "wikimedia.org",
    "architecturaldigest", "dezeen.com", "archdaily.com",
    "buildingmaterials", "constructionweek",
    "quora.com", "reddit.com", "medium.com",
    "blogger.com", "wordpress.com",
]


def _is_supplier_page(title: str, url: str) -> bool:
    """
    Return False if the page is clearly an article, guide, wiki, or
    price-comparison page rather than an actual supplier website.
    """
    title_lower = title.lower()
    url_lower   = url.lower()

    for pattern in _NON_SUPPLIER_TITLE_PATTERNS:
        if pattern in title_lower:
            return False

    for domain in _NON_SUPPLIER_DOMAINS:
        if domain in url_lower:
            return False

    return True


def _detect_country(text: str) -> str:
    lower = text.lower()
    india = any(k in lower for k in _INDIA_KEYWORDS)
    china = any(k in lower for k in _CHINA_KEYWORDS)
    if india and not china:
        return "India"
    if china and not india:
        return "China"
    if india and china:
        # whichever appears first
        idx_india = min((lower.find(k) for k in _INDIA_KEYWORDS if k in lower), default=9999)
        idx_china = min((lower.find(k) for k in _CHINA_KEYWORDS if k in lower), default=9999)
        return "India" if idx_india <= idx_china else "China"
    return "Unknown"


def _extract_price(
    text: str,
    rates: dict[str, float],
) -> tuple[str, Optional[float]]:
    """
    Returns (raw_price_text, estimated_price_in_USD).

    Detects the currency from the text window surrounding each match,
    validates against per-currency ACP sanity bounds, then converts to USD.
    For a range, returns the midpoint.
    """
    for match in _PRICE_PATTERN.finditer(text):
        # Group layout (see _PRICE_PATTERN):
        #   1 = range cur1 prefix (may be empty)
        #   2 = range lo number
        #   3 = range cur2 prefix (may be empty)
        #   4 = range hi number
        #   5 = single number (alternative branch)
        cur1_raw = (match.group(1) or "").strip()
        lo_s     = match.group(2)
        cur2_raw = (match.group(3) or "").strip()
        hi_s     = match.group(4)
        single_s = match.group(5)

        # Detect currency from the 30-char window before the match
        ctx_start = max(0, match.start() - 30)
        ctx       = text[ctx_start : match.end() + 10]
        cur       = _cur.detect_currency(ctx)
        lo_b, hi_b = _cur.price_bounds(cur)
        sym       = _cur.symbol(cur)

        if lo_s and hi_s:
            # Reject mixed-currency ranges like "$500 – 800 EUR" — both ends
            # must carry the same currency prefix (or neither).
            if cur1_raw and cur2_raw:
                if _cur.detect_currency(cur1_raw) != _cur.detect_currency(cur2_raw):
                    continue

            lo_f, hi_f = float(lo_s), float(hi_s)
            if lo_b <= lo_f <= hi_b and lo_b <= hi_f <= hi_b:
                mid     = round((lo_f + hi_f) / 2, 2)
                mid_usd = _cur.to_usd(mid, cur, rates)
                return (f"{sym}{lo_s}–{sym}{hi_s}/sqm", mid_usd)
        elif single_s:
            s = float(single_s)
            if lo_b <= s <= hi_b:
                usd = _cur.to_usd(s, cur, rates)
                return (f"{sym}{single_s}/sqm", usd)

    return ("Not found", None)


def _is_acp_relevant(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in _ACP_KEYWORDS)


def _extract_name(title: str, url: str) -> str:
    """Best-effort supplier name from title or domain."""
    # Strip common suffixes
    name = re.sub(
        r"\s*[\|\-–—]\s*.+$", "", title
    ).strip()
    if len(name) < 3:
        # fallback: use domain
        domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
        name = domain.split(".")[0].title()
    return name or "Unknown Supplier"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def clean_results(
    raw_results: list[dict],
    country_override: str = "",
    rates: dict[str, float] | None = None,
) -> list[SupplierRecord]:
    """
    Clean a list of raw Tavily results into SupplierRecord objects.

    Args:
        raw_results:      List of dicts from tavily_client.search_suppliers()
        country_override: Force "India" or "China" if already known from query

    Returns:
        List of SupplierRecord (ACP-relevant only, duplicates removed)
    """
    if rates is None:
        rates = _cur.get_rates()

    seen_urls: set[str] = set()
    records: list[SupplierRecord] = []

    for item in raw_results:
        title   = item.get("title", "")
        url     = item.get("url", "")
        content = item.get("content", "")
        _raw_score = item.get("score")
        score = float(_raw_score) if isinstance(_raw_score, (int, float)) else 0.0

        combined_text = f"{title} {content}"

        # Skip non-ACP results
        if not _is_acp_relevant(combined_text):
            continue

        # Skip articles, guides, price-comparison pages — must be a supplier site
        if not _is_supplier_page(title, url):
            continue

        # Deduplicate by URL
        if url in seen_urls:
            continue
        seen_urls.add(url)

        country = country_override or _detect_country(combined_text)
        name    = _extract_name(title, url)
        price_raw, price_est = _extract_price(combined_text, rates)

        records.append(SupplierRecord(
            name=name,
            country=country,
            url=url,
            description=content[:300].strip(),
            price_raw=price_raw,
            price_est=price_est,
            relevance_score=score,
            raw_content=content,
        ))

    return records
