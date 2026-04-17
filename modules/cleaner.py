"""
modules/cleaner.py

Data Intelligence Layer — hybrid rule-based + conditional AI processing.

Pipeline:
  1. Raw Tavily result
  2. Rule-based page filtering (deterministic, always runs)
  3. Rule-based extraction (name, price, signals — always runs)
  4. AI enhancement (CONDITIONAL — only for ambiguous cases)
  5. Structured SupplierRecord output

Core principle: rules handle all obvious cases; AI is only used for
ambiguous cases. The pipeline remains deterministic-first.

NEVER pass raw Tavily output downstream — always clean first.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from modules import currency as _cur

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SupplierRecord:
    name:            str
    country:         str                    # "India" | "China" | "Unknown"
    url:             str
    description:     str
    price_raw:       str                    # original price text (for display)
    price_est:       Optional[float]        # estimated USD/sqm (None if not found)
    relevance_score: float                  # Tavily relevance score (0-1)
    raw_content:     str                    # kept for risk scoring + AI prompts
    # --- Data Intelligence additions (defaults keep downstream compatible) ---
    supplier_type:   str = "unknown"        # manufacturer | reseller | trader | unknown
    signals:         dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Currency helpers (unchanged from previous version)
# ---------------------------------------------------------------------------

_CUR_PFX     = r"(?:₹|¥|€|\$|a\$|rs\.?\s*|usd\s*|inr\s*|cny\s*|rmb\s*|eur\s*|aud\s*)?"
_CUR_PFX_CAP = r"(₹|¥|€|\$|a\$|rs\.?\s*|usd\s*|inr\s*|cny\s*|rmb\s*|eur\s*|aud\s*)?"

_PRICE_PATTERN = re.compile(
    _CUR_PFX_CAP + r"(\d+(?:\.\d+)?)\s*[-–—to]+\s*" + _CUR_PFX_CAP + r"(\d+(?:\.\d+)?)"
    r"|"
    + _CUR_PFX + r"(\d+(?:\.\d+)?)\s*(?:usd|inr|cny|rmb|eur|aud|\/sqm|per\s*sqm|per\s*m2)?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

_INDIA_KEYWORDS  = ["india", "indian", "gujarat", "mumbai", "delhi", "chennai",
                     "rajkot", "ahmedabad", "pune", "hyderabad"]
_CHINA_KEYWORDS  = ["china", "chinese", "guangzhou", "shanghai", "beijing",
                     "shenzhen", "zhejiang", "fujian", "jiangsu"]
_ACP_KEYWORDS    = ["acp", "aluminium composite", "aluminum composite",
                     "alucobond", "alubond", "aludecor", "alstrong", "cladding"]


# ---------------------------------------------------------------------------
# STEP 1 — Rule-based page filtering (deterministic, always runs)
# ---------------------------------------------------------------------------

# Titles that signal non-supplier pages — checked as substring matches
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

# Exact title matches for generic non-supplier pages (case-insensitive)
_INVALID_TITLE_EXACT = {
    "about", "about us", "about me",
    "contact", "contact us",
    "blog", "news", "articles",
    "privacy", "privacy policy",
    "terms", "terms of service", "terms and conditions",
    "login", "sign in", "register", "sign up",
    "sitemap", "faq", "faqs",
    "careers", "jobs", "team",
}

# URL path segments that indicate non-product pages
_INVALID_URL_SEGMENTS = [
    "/about", "/contact", "/blog", "/news",
    "/privacy", "/terms", "/login", "/signin",
    "/register", "/careers", "/faq", "/sitemap",
]

# Domains that are content/news/wiki sites, never suppliers
_NON_SUPPLIER_DOMAINS = [
    "wikipedia.org", "wikimedia.org",
    "architecturaldigest", "dezeen.com", "archdaily.com",
    "buildingmaterials", "constructionweek",
    "quora.com", "reddit.com", "medium.com",
    "blogger.com", "wordpress.com",
]


def _is_supplier_page(title: str, url: str) -> bool:
    """
    Stage 1 filter: reject pages that are clearly NOT supplier product pages.

    Checks (in order):
      1. Exact title match against known invalid page types
      2. Title substring match against article/guide patterns
      3. URL path segment match against non-product segments
      4. URL domain match against known non-supplier domains
    """
    title_lower = title.lower().strip()
    url_lower   = url.lower()

    if title_lower in _INVALID_TITLE_EXACT:
        return False

    for pattern in _NON_SUPPLIER_TITLE_PATTERNS:
        if pattern in title_lower:
            return False

    for seg in _INVALID_URL_SEGMENTS:
        if seg in url_lower:
            return False

    for domain in _NON_SUPPLIER_DOMAINS:
        if domain in url_lower:
            return False

    return True


def _is_acp_relevant(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in _ACP_KEYWORDS)


# ---------------------------------------------------------------------------
# STEP 2 — Rule-based extraction
# ---------------------------------------------------------------------------

def _detect_country(text: str) -> str:
    lower = text.lower()
    india = any(k in lower for k in _INDIA_KEYWORDS)
    china = any(k in lower for k in _CHINA_KEYWORDS)
    if india and not china:
        return "India"
    if china and not india:
        return "China"
    if india and china:
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
        cur1_raw = (match.group(1) or "").strip()
        lo_s     = match.group(2)
        cur2_raw = (match.group(3) or "").strip()
        hi_s     = match.group(4)
        single_s = match.group(5)

        ctx_start = max(0, match.start() - 30)
        ctx       = text[ctx_start : match.end() + 10]
        cur       = _cur.detect_currency(ctx)
        lo_b, hi_b = _cur.price_bounds(cur)
        sym       = _cur.symbol(cur)

        if lo_s and hi_s:
            if cur1_raw and cur2_raw:
                if _cur.detect_currency(cur1_raw) != _cur.detect_currency(cur2_raw):
                    continue
            lo_f, hi_f = float(lo_s), float(hi_s)
            if lo_b <= lo_f <= hi_b and lo_b <= hi_f <= hi_b:
                mid     = round((lo_f + hi_f) / 2, 2)
                mid_usd = _cur.to_usd(mid, cur, rates)
                return (f"{sym}{lo_s}\u2013{sym}{hi_s}/sqm", mid_usd)
        elif single_s:
            s = float(single_s)
            if lo_b <= s <= hi_b:
                usd = _cur.to_usd(s, cur, rates)
                return (f"{sym}{single_s}/sqm", usd)

    return ("Not found", None)


def _extract_name(title: str, url: str) -> str:
    """Best-effort supplier name from title or domain."""
    name = re.sub(r"\s*[\|\-\u2013\u2014]\s*.+$", "", title).strip()
    if len(name) < 3:
        domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
        name = domain.split(".")[0].title()
    return name or "Unknown Supplier"


# --- Signal extraction (NEW) ---

_CERT_KEYWORDS = [
    "iso 9001", "iso 14001", "ce marking", "ce certified",
    "bv certified", "sgs", "tuv", "astm", "en 13501",
    "fire rated", "fire test", "b1 grade", "a2 grade",
    "rohs", "reach", "greenguard", "leed",
]
_CONTACT_RE = re.compile(
    r"\+?\d[\d\s\-()]{8,15}"   # phone
    r"|[\w.+-]+@[\w-]+\.[\w.]+"  # email
)
_MANUFACTURER_KEYWORDS = [
    "manufacturer", "manufacturing", "factory", "production line",
    "our plant", "our factory", "we produce", "we manufacture",
    "production capacity", "extrusion line",
]
_REVIEW_KEYWORDS = [
    "review", "reviews", "testimonial", "customer feedback",
    "rated", "rating", "stars", "verified buyer",
]


def _extract_signals(combined_text: str, raw_content: str) -> dict:
    """
    Extract structured quality signals from the page content.

    These signals feed into the downstream risk_scorer (already checked
    there for description quality, contact, etc.) but are now also
    surfaced on the SupplierRecord for the anomaly and AI layers to use.
    """
    lower   = combined_text.lower()
    raw_low = (raw_content or "").lower()

    return {
        "has_certification": any(k in raw_low for k in _CERT_KEYWORDS),
        "has_contact_info":  bool(_CONTACT_RE.search(raw_content or "")),
        "is_manufacturer":   any(k in raw_low for k in _MANUFACTURER_KEYWORDS),
        "has_reviews":       any(k in raw_low for k in _REVIEW_KEYWORDS),
        "content_length":    len(raw_content or ""),
    }


# --- Supplier type (rule-based first guess) ---

_RESELLER_KEYWORDS = [
    "reseller", "distributor", "dealer", "wholesaler",
    "supplier of", "we supply", "stockist",
]
_TRADER_KEYWORDS = [
    "trader", "trading company", "import export",
    "trading co", "general trading",
]


def _guess_supplier_type(raw_content: str) -> str:
    """Rule-based supplier type from page content."""
    low = (raw_content or "").lower()
    if any(k in low for k in _MANUFACTURER_KEYWORDS):
        return "manufacturer"
    if any(k in low for k in _RESELLER_KEYWORDS):
        return "reseller"
    if any(k in low for k in _TRADER_KEYWORDS):
        return "trader"
    return "unknown"


# ---------------------------------------------------------------------------
# STEP 3 — Extraction confidence scoring (determines whether AI is called)
# ---------------------------------------------------------------------------

# Patterns that indicate the name is generic / not a real company name
_GENERIC_NAME_PATTERNS = [
    re.compile(r"^about\b", re.I),
    re.compile(r"^contact\b", re.I),
    re.compile(r"^home\b", re.I),
    re.compile(r"^top\s+\d+", re.I),
    re.compile(r"^best\s+\d+", re.I),
    re.compile(r"^quality\s+", re.I),
    re.compile(r"^\d+\s+best\b", re.I),
    re.compile(r"^unknown\s+supplier$", re.I),
]

# Directory-like title patterns
_DIRECTORY_PATTERNS = [
    re.compile(r"top\s+\d+", re.I),
    re.compile(r"best\s+\d+", re.I),
    re.compile(r"\d+\s+best\b", re.I),
    re.compile(r"manufacturers?\s+(in|of|and|&)", re.I),
    re.compile(r"suppliers?\s+(in|of|and|&)", re.I),
    re.compile(r"companies\s+in", re.I),
    re.compile(r"brands?\s+(&|and)\s+companies", re.I),
]


def _is_generic_name(name: str) -> bool:
    """Return True if the extracted name looks generic / not a real company."""
    return any(p.search(name) for p in _GENERIC_NAME_PATTERNS)


def _looks_like_directory(title: str) -> bool:
    """Return True if the title pattern suggests a multi-supplier listing."""
    return any(p.search(title) for p in _DIRECTORY_PATTERNS)


def _extraction_confidence(name: str, title: str, url: str) -> float:
    """
    Heuristic confidence score for rule-based extraction quality.

    1.0 = clearly a specific supplier with a good name.
    0.0 = completely ambiguous, AI should definitely be called.
    """
    score = 1.0

    if _is_generic_name(name):
        score -= 0.5

    if _looks_like_directory(title):
        score -= 0.3

    if len(name) <= 3:
        score -= 0.2

    # Domain-based names are weak but usable
    domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
    domain_base = domain.split(".")[0].lower()
    if name.lower().replace(" ", "") == domain_base:
        score -= 0.1

    return max(0.0, min(1.0, round(score, 2)))


# ---------------------------------------------------------------------------
# STEP 4 — Conditional AI enhancement (only for ambiguous cases)
# ---------------------------------------------------------------------------

_AI_CONFIDENCE_THRESHOLD = 0.5  # call AI if extraction confidence is below this


# ---------------------------------------------------------------------------
# STEP 3.5 — POST-AI SAFETY FILTER (runs AFTER AI, non-negotiable)
#
# AI is advisory. Even if AI says page_type="supplier", these rule-based
# checks MUST still apply. Data quality > data quantity.
# ---------------------------------------------------------------------------

# Hard-reject keywords in the ORIGINAL title (not the AI-extracted name).
# Any title containing these is a directory/list page, period.
_DIRECTORY_TITLE_KEYWORDS = [
    re.compile(r"\btop\s+\d+\b", re.I),
    re.compile(r"\bbest\s+\d+\b", re.I),
    re.compile(r"\d+\s+best\b", re.I),
    re.compile(r"\blist\s+of\b", re.I),
    re.compile(r"\bmanufacturers\s+in\b", re.I),
    re.compile(r"\bsuppliers\s+in\b", re.I),
    re.compile(r"\bcompanies\s+in\b", re.I),
    re.compile(r"\bbrands\s+(&|and)\s+companies\b", re.I),
    re.compile(r"\btop\s+\w+\s+manufacturers\b", re.I),
    re.compile(r"\btop\s+\w+\s+suppliers\b", re.I),
]


def _is_directory_title(title: str) -> bool:
    """
    HARD rule-based filter — catches directory/list pages that AI may have
    misclassified as "supplier". Applied AFTER AI enhancement.

    This is the backstop: AI cannot override this check.
    """
    return any(p.search(title) for p in _DIRECTORY_TITLE_KEYWORDS)


def _is_valid_supplier(name: str, signals: dict, price_est: float | None) -> bool:
    """
    Require at least ONE positive indicator that this is a real supplier.

    A page with no company name, no contact info, and no pricing data is
    not a usable supplier entry — it's noise.
    """
    has_real_name    = not _is_generic_name(name) and len(name) >= 3
    has_contact      = bool(signals.get("has_contact_info"))
    has_price        = price_est is not None
    has_cert         = bool(signals.get("has_certification"))
    has_manufacturer = bool(signals.get("is_manufacturer"))

    return has_real_name or has_contact or (has_price and (has_cert or has_manufacturer))


def _needs_ai(name: str, title: str, url: str) -> bool:
    """
    Decide whether to invoke the AI enricher for this page.

    Returns True ONLY when at least one of:
      - Extracted name is generic ("About Us", "Top 10...", etc.)
      - Rule-based extraction confidence is low (< threshold)
      - Title looks like a directory listing

    Performance guard: most clean supplier pages pass through without an
    AI call. Only ambiguous edge cases trigger the API.
    """
    if _is_generic_name(name):
        return True
    if _looks_like_directory(title):
        return True
    if _extraction_confidence(name, title, url) < _AI_CONFIDENCE_THRESHOLD:
        return True
    return False


def _apply_ai_enhancement(
    title: str,
    url: str,
    content: str,
    rule_name: str,
    rule_supplier_type: str,
) -> tuple[str | None, str | None, str | None]:
    """
    Call the AI enricher and apply decision rules to the result.

    Returns (new_name, new_supplier_type, page_type) or (None, None, None)
    if AI is unavailable, fails, or returns low confidence.

    Decision rules:
      - page_type "invalid" or "directory" → return page_type so caller drops
      - confidence < 0.6 → ignore AI output entirely
      - Otherwise → merge company_name and supplier_type
    """
    try:
        from modules.ai_enricher import enhance_with_ai
    except ImportError:
        return (None, None, None)

    result = enhance_with_ai(title, url, content)
    if result is None:
        return (None, None, None)

    page_type  = result.get("page_type", "supplier")
    confidence = result.get("confidence", 0.0)

    # Drop pages AI classifies as invalid or directory
    if page_type in ("invalid", "directory"):
        return (None, None, page_type)

    # Ignore low-confidence AI output — keep rule-based data
    if confidence < 0.6:
        return (None, None, None)

    new_name = result.get("company_name", "").strip()
    if not new_name or new_name.upper() == "N/A" or len(new_name) < 2:
        new_name = None

    new_type = result.get("supplier_type", "").strip().lower()
    if new_type not in ("manufacturer", "reseller", "trader"):
        new_type = None

    return (new_name, new_type, page_type)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_AI_ENRICH_WORKERS = 4


def clean_results(
    raw_results: list[dict],
    country_override: str = "",
    rates: dict[str, float] | None = None,
    use_ai: bool = True,
) -> list[SupplierRecord]:
    """
    Clean raw Tavily results into SupplierRecord objects.

    Three-phase pipeline for performance:
      Phase 1 — Rule-based filtering + extraction (fast, no API calls)
      Phase 2 — AI enrichment in PARALLEL (only if use_ai=True, for ambiguous items)
      Phase 3 — Post-AI safety filter + SupplierRecord assembly

    use_ai=False skips Phase 2 entirely — used by Mode 01 (rule-only) for speed.
    """
    if rates is None:
        rates = _cur.get_rates()

    # === PHASE 1: Rule-based filter + extract (fast, no I/O) ===
    t0 = time.time()
    seen_urls: set[str] = set()
    candidates: list[dict] = []

    for item in raw_results:
        title   = item.get("title", "")
        url     = item.get("url", "")
        content = item.get("content", "")
        _raw_score = item.get("score")
        score = float(_raw_score) if isinstance(_raw_score, (int, float)) else 0.0

        combined_text = f"{title} {content}"

        if not _is_acp_relevant(combined_text):
            continue
        if not _is_supplier_page(title, url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        country       = country_override or _detect_country(combined_text)
        name          = _extract_name(title, url)
        price_raw, price_est = _extract_price(combined_text, rates)
        signals       = _extract_signals(combined_text, content)
        supplier_type = _guess_supplier_type(content)

        candidates.append({
            "title": title, "url": url, "content": content, "score": score,
            "country": country, "name": name,
            "price_raw": price_raw, "price_est": price_est,
            "signals": signals, "supplier_type": supplier_type,
            "needs_ai": _needs_ai(name, title, url),
        })

    t_phase1 = time.time() - t0

    # === PHASE 2: AI enrichment — PARALLEL for all ambiguous items ===
    t1 = time.time()
    ai_indices = [i for i, c in enumerate(candidates) if c["needs_ai"]] if use_ai else []

    if ai_indices:
        def _enrich(idx: int) -> tuple[int, tuple]:
            c = candidates[idx]
            return (idx, _apply_ai_enhancement(
                c["title"], c["url"], c["content"], c["name"], c["supplier_type"],
            ))

        workers = min(_AI_ENRICH_WORKERS, len(ai_indices))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for idx, ai_result in ex.map(_enrich, ai_indices):
                candidates[idx]["ai_result"] = ai_result

    t_phase2 = time.time() - t1

    # === PHASE 3: Apply AI + safety filter + build records ===
    records: list[SupplierRecord] = []

    for c in candidates:
        name          = c["name"]
        supplier_type = c["supplier_type"]

        ai = c.get("ai_result")
        if ai:
            ai_name, ai_type, ai_page_type = ai
            if ai_page_type in ("invalid", "directory", "article"):
                continue
            if ai_name:
                name = ai_name
            if ai_type:
                supplier_type = ai_type

        # Post-AI safety filter (non-negotiable)
        if _is_directory_title(c["title"]):
            continue
        if not _is_valid_supplier(name, c["signals"], c["price_est"]):
            continue

        records.append(SupplierRecord(
            name=name,
            country=c["country"],
            url=c["url"],
            description=c["content"][:300].strip(),
            price_raw=c["price_raw"],
            price_est=c["price_est"],
            relevance_score=c["score"],
            raw_content=c["content"],
            supplier_type=supplier_type,
            signals=c["signals"],
        ))

    _log.info(
        "[clean] phase1=%.2fs  phase2_ai=%.2fs (%d calls)  output=%d records",
        t_phase1, t_phase2, len(ai_indices), len(records),
    )

    return records
