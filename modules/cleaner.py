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

_PRICE_PATTERN = re.compile(
    r"\$?\s*(\d+(?:\.\d+)?)\s*[-–—to]+\s*\$?\s*(\d+(?:\.\d+)?)"  # range: 14–18
    r"|\$?\s*(\d+(?:\.\d+)?)\s*(?:usd|\/sqm|per\s*sqm|per\s*m2)?",  # single: $15
    re.IGNORECASE,
)

_INDIA_KEYWORDS  = ["india", "indian", "gujarat", "mumbai", "delhi", "chennai",
                     "rajkot", "ahmedabad", "pune", "hyderabad"]
_CHINA_KEYWORDS  = ["china", "chinese", "guangzhou", "shanghai", "beijing",
                     "shenzhen", "zhejiang", "fujian", "jiangsu"]
_ACP_KEYWORDS    = ["acp", "aluminium composite", "aluminum composite",
                     "alucobond", "alubond", "aludecor", "alstrong", "cladding"]


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


def _extract_price(text: str) -> tuple[str, Optional[float]]:
    """
    Returns (raw_price_text, estimated_float_value).
    For a range, returns the midpoint.
    """
    for match in _PRICE_PATTERN.finditer(text):
        lo, hi, single = match.group(1), match.group(2), match.group(3)
        if lo and hi:
            lo_f, hi_f = float(lo), float(hi)
            # sanity check: ACP is typically $5–$50/sqm
            if 3 <= lo_f <= 100 and 3 <= hi_f <= 100:
                mid = round((lo_f + hi_f) / 2, 2)
                return (f"${lo}–${hi}/sqm", mid)
        elif single:
            s = float(single)
            if 3 <= s <= 100:
                return (f"${single}/sqm", s)
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
) -> list[SupplierRecord]:
    """
    Clean a list of raw Tavily results into SupplierRecord objects.

    Args:
        raw_results:      List of dicts from tavily_client.search_suppliers()
        country_override: Force "India" or "China" if already known from query

    Returns:
        List of SupplierRecord (ACP-relevant only, duplicates removed)
    """
    seen_urls: set[str] = set()
    records: list[SupplierRecord] = []

    for item in raw_results:
        title   = item.get("title", "")
        url     = item.get("url", "")
        content = item.get("content", "")
        score   = float(item.get("score", 0.0))

        combined_text = f"{title} {content}"

        # Skip non-ACP results
        if not _is_acp_relevant(combined_text):
            continue

        # Deduplicate by URL
        if url in seen_urls:
            continue
        seen_urls.add(url)

        country = country_override or _detect_country(combined_text)
        name    = _extract_name(title, url)
        price_raw, price_est = _extract_price(combined_text)

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
