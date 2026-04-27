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
from urllib.parse import urlparse

from modules import currency as _cur

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SupplierRecord:
    name:            str
    country:         str                    # canonical country name, e.g. "China", "Vietnam", "Unknown"
    url:             str
    description:     str
    price_raw:       str                    # original price text (for display)
    price_est:       Optional[float]        # estimated USD per detected unit (None if not found)
    relevance_score: float                  # Tavily/Serper relevance score (0-1)
    raw_content:     str                    # kept for risk scoring + AI prompts
    # --- Data Intelligence additions (defaults keep downstream compatible) ---
    supplier_type:   str = "unknown"        # manufacturer | reseller | trader | unknown
    signals:         dict = field(default_factory=dict)
    # --- Multi-metal / multi-unit additions ---
    category:        str = "unknown"        # acp | aluminum | steel | stainless_steel | copper | ...
    price_unit:      str = "unknown"        # sqm | ton | kg | meter | piece | ft | unknown
    price_unit_source: str = "unknown"      # regex (high) | keyword (med) | category (low) | unknown
    price_original:  str = ""               # raw amount in original currency + unit, e.g. "CNY 25000/ton"


# ---------------------------------------------------------------------------
# Currency helpers (unchanged from previous version)
# ---------------------------------------------------------------------------

_CUR_PFX     = r"(?:₹|¥|€|\$|a\$|rs\.?\s*|usd\s*|inr\s*|cny\s*|rmb\s*|eur\s*|aud\s*)?"
_CUR_PFX_CAP = r"(₹|¥|€|\$|a\$|rs\.?\s*|usd\s*|inr\s*|cny\s*|rmb\s*|eur\s*|aud\s*)?"

# Unit token (no capture group — name-captured by the wrapping pattern).
_UNIT_TOKENS = (
    r"(?:per\s*)?(?:/\s*)?"
    r"(?:sqm|m2|m\xb2|tonne|metric\s*ton|ton|mt|kg|piece|pcs|pc|meter|metre|ft|foot|m\b|t\b)"
)

# Currency prefix token, no capture (used in named-group wrappers).
_CUR_TOKEN = r"(?:₹|\xa5|€|\$|a\$|rs\.?\s*|usd\s*|inr\s*|cny\s*|rmb\s*|eur\s*|aud\s*)"

# Named-group price pattern. Two alternatives:
#   1. Range:  [cur]?<lo> - [cur]?<hi> [unit]?
#   2. Single: [cur]?<single> [unit]?
# Unit tokens are captured by name so _detect_unit_from_match() can read them
# without depending on positional group numbering.
_PRICE_PATTERN = re.compile(
    r"(?P<cur_lo>" + _CUR_TOKEN + r")?"
    r"(?P<lo>\d+(?:\.\d+)?)\s*[-–—]+\s*"
    r"(?P<cur_hi>" + _CUR_TOKEN + r")?"
    r"(?P<hi>\d+(?:\.\d+)?)"
    r"\s*(?P<unit_range>" + _UNIT_TOKENS + r")?"
    r"|"
    r"(?P<cur_s>" + _CUR_TOKEN + r")?"
    r"(?P<single>\d+(?:\.\d+)?)"
    r"\s*(?:usd|inr|cny|rmb|eur|aud)?"
    r"\s*(?P<unit_single>" + _UNIT_TOKENS + r")?",
    re.IGNORECASE,
)

# Map matched unit token to canonical unit name.
_UNIT_CANONICAL: dict[str, str] = {
    "sqm": "sqm", "m2": "sqm", "m²": "sqm",
    "ton": "ton", "tonne": "ton", "metric ton": "ton", "mt": "ton", "t": "ton",
    "kg": "kg",
    "piece": "piece", "pc": "piece", "pcs": "piece",
    "meter": "meter", "metre": "meter", "m": "meter",
    "ft": "ft", "foot": "ft",
}

# Default unit per category, used when no unit is found by regex or keyword.
_CATEGORY_DEFAULT_UNIT: dict[str, str] = {
    "acp":             "sqm",
    "aluminum":        "ton",
    "steel":           "ton",
    "stainless_steel": "ton",
    "copper":          "ton",
    "brass":           "ton",
    "zinc":            "ton",
    "titanium":        "kg",
    "tube":            "meter",
    "pipe":            "meter",
    "unknown":         "unknown",
}

# Whitelist of plausible price units per category. Without this guard, the
# regex happily reads "Width: 2 meter" as "$2/meter" for an ACP supplier,
# producing a $0.02/sqm-equivalent number that's clearly garbage. Empty set
# (or category not in map) = no filter (accept anything).
_CATEGORY_VALID_UNITS: dict[str, set[str]] = {
    "acp":             {"sqm"},                       # panels: per square meter
    "aluminum":        {"sqm", "ton", "kg"},          # sheet/plate/coil
    "steel":           {"ton", "kg"},
    "stainless_steel": {"sqm", "ton", "kg"},
    "copper":          {"ton", "kg"},
    "brass":           {"ton", "kg"},
    "zinc":            {"ton", "kg", "sqm"},          # zinc sheet exists
    "titanium":        {"kg", "ton"},
    "tube":            {"meter", "ft", "kg"},
    "pipe":            {"meter", "ft", "kg"},
    "unknown":         set(),                          # no filter
}

# Page-text keyword hints that imply a unit when no regex match is found.
# E.g. a page about "sheet" almost always quotes per-sqm; a page about "tube"
# is almost always per-meter; "coil" or "ton" reliably implies per-ton pricing.
_UNIT_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(per\s*sqm|per\s*m2|per\s*square\s*meter|/\s*sqm|/\s*m2)\b", re.I), "sqm"),
    (re.compile(r"\b(per\s*ton|per\s*tonne|per\s*mt|/\s*ton|/\s*mt|usd/?ton)\b", re.I), "ton"),
    (re.compile(r"\b(per\s*kg|/\s*kg)\b", re.I), "kg"),
    (re.compile(r"\b(per\s*meter|per\s*metre|/\s*m\b|/\s*meter)\b", re.I), "meter"),
    (re.compile(r"\b(per\s*piece|per\s*pc|/\s*piece|/\s*pc)\b", re.I), "piece"),
    (re.compile(r"\b(sheet|panel|cladding)s?\b", re.I), "sqm"),
    (re.compile(r"\b(tube|tubing|pipe|piping|bar|rod)s?\b", re.I), "meter"),
    (re.compile(r"\b(coil|ingot|billet)s?\b", re.I), "ton"),
]


# ---------------------------------------------------------------------------
# Country detection — URL ccTLD has priority over text scanning because it
# is a much higher-fidelity signal: a `.cn` or `.kr` domain almost always
# corresponds to a Chinese / Korean supplier, even when the title and
# snippet don't mention the country in text.
# ---------------------------------------------------------------------------

# ccTLD -> canonical country name (must match _COUNTRY_KEYWORDS canon names).
# `.com.cn`, `.co.kr`, `.com.au` etc. all match via host.endswith(".cn"|".kr"|".au").
_COUNTRY_TLD: dict[str, str] = {
    ".cn": "China",
    ".in": "India",
    ".vn": "Vietnam",
    ".kr": "South Korea",
    ".jp": "Japan",
    ".tw": "Taiwan",
    ".tr": "Turkey",
    ".th": "Thailand",
    ".my": "Malaysia",
    ".id": "Indonesia",
    ".de": "Germany",
    ".it": "Italy",
    ".us": "United States",
    ".ae": "United Arab Emirates",
    ".sa": "Saudi Arabia",
    ".au": "Australia",
    # +12 metals-relevant additions
    ".br": "Brazil",
    ".cl": "Chile",
    ".pe": "Peru",
    ".mx": "Mexico",
    ".ca": "Canada",
    ".ru": "Russia",
    ".za": "South Africa",
    ".eg": "Egypt",
    ".es": "Spain",
    ".pl": "Poland",
    ".fr": "France",
    ".uk": "United Kingdom",
}


def _detect_country_from_url(url: str) -> str:
    """
    Country from URL ccTLD. Returns "" when no recognized ccTLD is found —
    e.g. .com / .net domains, where text scanning is the only option.
    """
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if not host:
        return ""
    for tld, country in _COUNTRY_TLD.items():
        if host.endswith(tld):
            return country
    return ""


# ---------------------------------------------------------------------------
# Country keyword dictionary — mirrors engine.query_parser._COUNTRY_KW
# ---------------------------------------------------------------------------

# Each (canonical_name, [keyword_substrings]). Matching is substring-based
# and case-insensitive. Used by _detect_country() below.
_COUNTRY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("China",                ["china", "chinese", "guangzhou", "shanghai", "beijing",
                              "shenzhen", "zhejiang", "fujian", "jiangsu"]),
    ("India",                ["india", "indian", "gujarat", "mumbai", "delhi", "chennai",
                              "rajkot", "ahmedabad", "pune", "hyderabad"]),
    ("Vietnam",              ["vietnam", "vietnamese", "ho chi minh", "hanoi"]),
    ("South Korea",          ["south korea", " korea", "korean", "seoul"]),
    ("Japan",                ["japan", "japanese", "tokyo", "osaka"]),
    ("Taiwan",               ["taiwan", "taiwanese", "taipei"]),
    ("Turkey",               ["turkey", "turkish", "istanbul"]),
    ("Thailand",             ["thailand", "thai", "bangkok"]),
    ("Malaysia",             ["malaysia", "malaysian", "kuala lumpur"]),
    ("Indonesia",            ["indonesia", "indonesian", "jakarta"]),
    ("Germany",              ["germany", "german", "deutschland"]),
    ("Italy",                ["italy", "italian"]),
    ("United States",        ["united states", "u.s.a", "usa", "american"]),
    ("United Arab Emirates", ["united arab emirates", "u.a.e", "uae", "dubai"]),
    ("Saudi Arabia",         ["saudi arabia", "saudi", "riyadh"]),
    ("Australia",            ["australia", "australian", "sydney", "melbourne"]),
    # --- +12 metals-relevant additions ---
    # Avoid 2-letter ambiguous keywords (e.g. "uk" matches "duke") and
    # avoid words that collide with our domain (e.g. "polish" collides
    # with the "polished" finish keyword in _VARIANT_KW).
    ("Brazil",         ["brazil", "brazilian", "sao paulo", "rio de janeiro"]),
    ("Chile",          ["chilean", "santiago", "antofagasta"]),  # "chile" alone is too generic (chili pepper)
    ("Peru",           ["peruvian", "lima"]),                    # "peru" alone risks false positives
    ("Mexico",         ["mexico", "mexican", "monterrey", "guadalajara"]),
    ("Canada",         ["canada", "canadian", "toronto", "montreal", "vancouver"]),
    ("Russia",         ["russia", "russian", "moscow", "st petersburg"]),
    ("South Africa",   ["south africa", "south african", "johannesburg",
                        "cape town", "durban"]),
    ("Egypt",          ["egypt", "egyptian", "cairo", "alexandria"]),
    ("Spain",          ["spain", "spanish", "madrid", "barcelona"]),
    ("Poland",         ["poland", "warsaw", "krakow"]),          # NOT "polish" (variant collision)
    ("France",         ["france", "french", "paris", "lyon", "marseille"]),
    ("United Kingdom", ["united kingdom", "great britain", "u.k.",
                        "england", "london", "manchester", "sheffield"]),
]


# ---------------------------------------------------------------------------
# Category keyword filter — replaces the old ACP-only _ACP_KEYWORDS check.
# Each value is the keyword set that signals the page is about that category.
# An empty list means "accept everything" (used for category="unknown").
# ---------------------------------------------------------------------------

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "acp":             ["acp", "aluminium composite", "aluminum composite",
                        "alucobond", "alubond", "aludecor", "alstrong", "cladding"],
    "aluminum":        ["aluminium", "aluminum", "6061", "6063", "7075", "5052"],
    "steel":           ["steel", "carbon steel", "mild steel", "a36", "q235", "s235"],
    "stainless_steel": ["stainless", "ss sheet", "ss plate", "304", "316", "430"],
    "copper":          ["copper", "c11000", "electrolytic copper"],
    "brass":           ["brass"],
    "zinc":            ["zinc", "galvanized", "galvanised"],
    "titanium":        ["titanium"],
    "tube":            ["tube", "tubing"],
    "pipe":            ["pipe", "piping"],
    "unknown":         [],   # no filter
}


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
    # Market-research / analyst-report pages — these scrape clean as
    # "suppliers" but are content articles. "analysis 202" matches
    # "analysis 2024", "analysis 2025", etc. through normal substring match.
    "market report", "industry report", "market size",
    "market share", "market analysis", "market outlook",
    "forecast", "research report", "analysis 202",
    "key players", "market trends",
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
    # Analyst/research-content paths — same family as the title patterns above.
    "/report/", "/reports/", "/insights/", "/analysis/",
    "/market-report", "/research-report",
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


def _is_category_relevant(text: str, category: str) -> bool:
    """
    Stage-1 filter: keep pages whose text mentions at least one keyword for
    the requested category. category="unknown" disables the filter (used for
    chat queries that didn't specify a metal family).
    """
    keywords = _CATEGORY_KEYWORDS.get(category, [])
    if not keywords:
        return True
    lower = text.lower()
    return any(k in lower for k in keywords)


# ---------------------------------------------------------------------------
# STEP 2 — Rule-based extraction
# ---------------------------------------------------------------------------

def _detect_country(text: str) -> str:
    """
    Return canonical country name from page text. If multiple match, the one
    whose first keyword appears earliest wins (closest to the lede).
    """
    lower = text.lower()
    best: tuple[int, str] | None = None
    for canon, kws in _COUNTRY_KEYWORDS:
        positions = [lower.find(k) for k in kws if k in lower]
        if not positions:
            continue
        first = min(positions)
        if best is None or first < best[0]:
            best = (first, canon)
    return best[1] if best else "Unknown"


def _normalize_unit_token(tok: str) -> str:
    """Strip 'per ' / '/' wrapper and look up canonical unit. '' on failure."""
    if not tok:
        return ""
    cleaned = tok.lower().strip()
    cleaned = re.sub(r"^per\s+", "", cleaned)
    cleaned = cleaned.lstrip("/").strip()
    cleaned = " ".join(cleaned.split())
    return _UNIT_CANONICAL.get(cleaned, "")


def _detect_unit_from_match(match, surrounding_text: str, category: str) -> tuple[str, str]:
    """
    Returns (unit, source) where source is one of:
      "regex"    \u2014 captured directly inside the price match
      "keyword"  \u2014 inferred from page-text hints (sheet -> sqm, tube -> meter)
      "category" \u2014 fallback to the per-category default
      "unknown"  \u2014 no signal at all
    """
    for name in ("unit_range", "unit_single"):
        tok = match.groupdict().get(name)
        canon = _normalize_unit_token(tok or "")
        if canon:
            return (canon, "regex")

    for pat, unit in _UNIT_HINTS:
        if pat.search(surrounding_text):
            return (unit, "keyword")

    default = _CATEGORY_DEFAULT_UNIT.get(category, "unknown")
    if default != "unknown":
        return (default, "category")
    return ("unknown", "unknown")


def _extract_price(
    text: str,
    rates: dict[str, float],
    category: str = "unknown",
) -> tuple[str, Optional[float], str, str, str]:
    """
    Extract a price from page text.

    Returns:
        (raw_price_text, price_est_usd, price_unit, price_unit_source,
         price_original)

    - `raw_price_text`     display string with currency symbol + range
    - `price_est_usd`      float USD per detected unit, or None
    - `price_unit`         sqm | ton | kg | meter | piece | ft | unknown
    - `price_unit_source`  regex | keyword | category | unknown
    - `price_original`     short string preserving currency + amount + unit,
                           e.g. "CNY 25000-28000/ton"

    Bounds checking:
      - For category=acp + unit=sqm we still apply the strict per-currency
        ACP sqm bounds (defended legacy behavior).
      - For other (category, unit) combos, we accept any positive amount
        and let value_scorer apply per-bucket sanity downstream.
    """
    not_found = ("Not found", None, "unknown", "unknown", "")

    for match in _PRICE_PATTERN.finditer(text):
        gd       = match.groupdict()
        cur1_raw = (gd.get("cur_lo") or "").strip()
        lo_s     = gd.get("lo")
        cur2_raw = (gd.get("cur_hi") or "").strip()
        hi_s     = gd.get("hi")
        single_s = gd.get("single")
        cur_s_raw = (gd.get("cur_s")  or "").strip()
        unit_range_tok  = gd.get("unit_range")
        unit_single_tok = gd.get("unit_single")

        # Reject matches that have neither a currency prefix nor a regex-
        # captured unit. Without one of these signals a bare number is almost
        # always something else (alloy code, year, dimension, etc.).
        is_range = bool(lo_s and hi_s)
        if is_range:
            if not (cur1_raw or cur2_raw or unit_range_tok):
                continue
        else:
            if not (cur_s_raw or unit_single_tok):
                continue

        ctx_start = max(0, match.start() - 30)
        ctx       = text[ctx_start : match.end() + 30]
        cur       = _cur.detect_currency(ctx)
        sym       = _cur.symbol(cur)

        unit, unit_src = _detect_unit_from_match(match, ctx, category)

        # Reject units that don't make sense for this category. E.g. ACP is
        # only sold per sqm — if the regex picked up "/meter" or "/kg" from
        # surrounding text, this match is almost certainly garbage (a
        # dimension or weight, not a price). Skip and try the next match.
        valid_units = _CATEGORY_VALID_UNITS.get(category)
        if valid_units and unit not in valid_units and unit != "unknown":
            continue

        is_acp_sqm     = (category == "acp" and unit == "sqm")
        lo_b, hi_b     = _cur.price_bounds(cur)
        unit_disp      = unit if unit != "unknown" else "unit"

        if lo_s and hi_s:
            if cur1_raw and cur2_raw:
                if _cur.detect_currency(cur1_raw) != _cur.detect_currency(cur2_raw):
                    continue
            try:
                lo_f, hi_f = float(lo_s), float(hi_s)
            except ValueError:
                continue
            if lo_f <= 0 or hi_f <= 0:
                continue
            if is_acp_sqm and not (lo_b <= lo_f <= hi_b and lo_b <= hi_f <= hi_b):
                continue
            mid     = round((lo_f + hi_f) / 2, 2)
            mid_usd = _cur.to_usd(mid, cur, rates)
            raw     = f"{sym}{lo_s}\u2013{sym}{hi_s}/{unit_disp}"
            orig    = f"{cur} {lo_s}-{hi_s}/{unit_disp}"
            return (raw, mid_usd, unit, unit_src, orig)

        if single_s:
            try:
                s = float(single_s)
            except ValueError:
                continue
            if s <= 0:
                continue
            if is_acp_sqm and not (lo_b <= s <= hi_b):
                continue
            usd = _cur.to_usd(s, cur, rates)
            raw = f"{sym}{single_s}/{unit_disp}"
            orig = f"{cur} {single_s}/{unit_disp}"
            return (raw, usd, unit, unit_src, orig)

    return not_found


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

# Scale hints — weak signals that the supplier is a large-scale operation,
# which usually implies better unit pricing. Used by the price estimator.
_SCALE_PATTERNS: list[tuple] = [
    (re.compile(r"(\d{2,6})\s*(?:\+\s*)?(?:workers|employees|staff|people)", re.I), "workers"),
    (re.compile(r"(\d{2,6}(?:,\d{3})?)\s*(?:sqm|square\s*meters?|m\xb2|m2)\s*(?:factory|plant|facility|workshop|site|area)?", re.I), "area_sqm"),
    (re.compile(r"(\d{2,6}(?:,\d{3})?)\s*(?:tons?|tonnes?|mt)\s*(?:per\s*year|per\s*annum|annual|annually|/\s*year|/\s*yr)", re.I), "annual_ton"),
    (re.compile(r"(\d{1,3}(?:,\d{3})?)\s*(?:sqm|square\s*meters?|m2)\s*(?:per\s*day|/\s*day|daily)", re.I), "daily_sqm"),
]


def _extract_scale_hint(raw_content: str) -> dict:
    """
    Return a dict of scale signals extracted from the page. All values are
    integers; keys are "workers" / "area_sqm" / "annual_ton" / "daily_sqm".
    Missing keys mean "not found" (not "zero"). The estimator applies small
    discounts when any of these exceed its thresholds.
    """
    result: dict = {}
    for pat, kind in _SCALE_PATTERNS:
        m = pat.search(raw_content or "")
        if not m:
            continue
        try:
            val = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # Sanity: workers in [10, 99999]; area in [100, 999999]; annual ton in [10, 99999]
        if kind == "workers" and not (10 <= val <= 99999):
            continue
        if kind == "area_sqm" and not (100 <= val <= 999999):
            continue
        if kind == "annual_ton" and not (10 <= val <= 999999):
            continue
        if kind == "daily_sqm" and not (10 <= val <= 99999):
            continue
        result[kind] = val
    return result


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
        "scale_hint":        _extract_scale_hint(raw_content or ""),
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
    category: str = "acp",
    allowed_countries: list[str] | None = None,
) -> list[SupplierRecord]:
    """
    Clean raw search results into SupplierRecord objects.

    Three-phase pipeline for performance:
      Phase 1 — Rule-based filtering + extraction (fast, no API calls)
      Phase 2 — AI enrichment in PARALLEL (only if use_ai=True, for ambiguous items)
      Phase 3 — Post-AI safety filter + SupplierRecord assembly

    use_ai=False skips Phase 2 entirely — used by Mode 01 (rule-only) for speed.

    `category` controls the relevance filter and the price-unit defaults.
    Defaults to "acp" for backward compatibility with the existing /api/analyze
    endpoint; new chat-driven flows should pass the parsed category explicitly.
    Use category="unknown" to disable category filtering entirely.

    `allowed_countries` is the strict country filter for the chat path. When
    set, only suppliers whose detected country is in the list survive. Pass
    None or [] to disable filtering (global searches and the legacy flow,
    which already pre-filters per country at the search layer).

    Independent of `allowed_countries`, suppliers with country="Unknown" are
    ALWAYS dropped — without a country we can't help the user actually source
    from them, so the listing has no procurement value.
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

        if not _is_category_relevant(combined_text, category):
            continue
        if not _is_supplier_page(title, url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Country: prefer URL ccTLD (high confidence) over text scan (low).
        country = (country_override
                   or _detect_country_from_url(url)
                   or _detect_country(combined_text))

        # Always drop Unknown-country results: if we can't tell where a
        # supplier is, the user can't contact / source from them, so the
        # listing has no procurement value regardless of how good the score
        # might look.
        if country == "Unknown":
            continue

        # Strict country filter for the chat path: drop anything that doesn't
        # match the user's specified countries.
        if allowed_countries and country not in allowed_countries:
            continue

        name          = _extract_name(title, url)
        price_raw, price_est, price_unit, price_unit_src, price_original = \
            _extract_price(combined_text, rates, category)

        # --- Bug A fix: kill obviously absurd extracted prices ---------
        # E.g. regex picks "$0.01/ton" out of a copper page from a sort
        # key or product code. Anything outside [5%, 2000%] of the
        # category midpoint is treated as "no price" so the supplier
        # falls into the model-estimate path instead of dragging price
        # rankings with a fake-cheap number.
        from engine.price_estimator import is_extracted_price_sane
        if price_est is not None and not is_extracted_price_sane(
            price_est, category, price_unit
        ):
            _log.info(
                "[clean] dropped insane price %s/%s for category=%s (page=%s)",
                price_est, price_unit, category, url[:60],
            )
            price_raw = "Not found"
            price_est = None
            price_unit = "unknown"
            price_unit_src = "unknown"
            price_original = ""

        signals       = _extract_signals(combined_text, content)
        supplier_type = _guess_supplier_type(content)

        candidates.append({
            "title": title, "url": url, "content": content, "score": score,
            "country": country, "name": name,
            "price_raw": price_raw, "price_est": price_est,
            "price_unit": price_unit, "price_unit_source": price_unit_src,
            "price_original": price_original,
            "signals": signals, "supplier_type": supplier_type,
            "needs_ai": _needs_ai(name, title, url),
            # Pass-through any per-result enrichment from upstream
            "angles_matched": item.get("angles_matched", []),
            "angle_count":    item.get("angle_count", 0),
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

        # Surface multi-angle metadata onto the record's signals dict so the
        # downstream scorer / frontend can read it without growing yet another
        # field. Keeps SupplierRecord's typed fields focused on the essentials.
        signals_out = dict(c["signals"])
        if c.get("angle_count"):
            signals_out["angle_count"]    = c["angle_count"]
            signals_out["angles_matched"] = c["angles_matched"]

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
            signals=signals_out,
            category=category,
            price_unit=c["price_unit"],
            price_unit_source=c["price_unit_source"],
            price_original=c["price_original"],
        ))

    _log.info(
        "[clean] phase1=%.2fs  phase2_ai=%.2fs (%d calls)  output=%d records",
        t_phase1, t_phase2, len(ai_indices), len(records),
    )

    return records
