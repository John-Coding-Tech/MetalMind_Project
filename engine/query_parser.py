"""
engine/query_parser.py

Natural-language query parser for the chat-style search interface.

Three-layer fallback pipeline:
    1. LLM (Gemma)      — full structured extraction from raw user message
    2. Regex fallback   — keyword-based extraction when LLM is unavailable
    3. Raw passthrough  — last resort, returns raw_query with category=unknown

Public API:
    parse_search_query(user_msg: str) -> dict

Output schema (always returned, even on failure):
    {
        "category":       str,    # canonical product family, e.g. "acp", "steel"
        "material":       str,    # specific grade/alloy if mentioned, else ""
        "variant":        str,    # surface finish (marble/brushed/...) or ""
        "countries":      list,   # canonical country names, e.g. ["China", "India"]
        "supplier_names": list,   # explicit company names, if any
        "price_range":    dict,   # {min, max, currency, unit}, fields may be None
        "spec":           str,    # thickness/grade/standard text
        "quantity":       dict,   # {value, unit} or None
        "raw_query":      str,    # original user input (for debug + Layer 3)
        "source":         str,    # "llm" | "regex" | "raw"
        "needs_clarification":   bool,
        "clarification_question": str,
    }

Determinism: regex layer is fully deterministic. LLM layer uses temperature=0
(see engine.ai_engine.call_model) so its output is also stable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from engine.ai_engine import call_model_fast, _extract_json

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical dictionaries (shared between LLM prompt and regex fallback)
# ---------------------------------------------------------------------------

# Country name → canonical form. Keys are lowercase substrings to match against
# the user message; the value is the display name we store downstream.
_COUNTRY_KW: dict[str, str] = {
    # China
    "china": "China", "chinese": "China", "prc": "China", "中国": "China",
    "guangzhou": "China", "shanghai": "China", "shenzhen": "China",
    "zhejiang": "China", "jiangsu": "China", "fujian": "China",
    # India
    "india": "India", "indian": "India", "印度": "India",
    "mumbai": "India", "delhi": "India", "gujarat": "India",
    "rajkot": "India", "ahmedabad": "India",
    # Vietnam
    "vietnam": "Vietnam", "vietnamese": "Vietnam", "越南": "Vietnam",
    "ho chi minh": "Vietnam", "hanoi": "Vietnam",
    # South Korea
    "korea": "South Korea", "south korea": "South Korea", "korean": "South Korea",
    "韩国": "South Korea", "seoul": "South Korea",
    # Japan
    "japan": "Japan", "japanese": "Japan", "日本": "Japan", "tokyo": "Japan",
    # Taiwan
    "taiwan": "Taiwan", "taiwanese": "Taiwan", "台湾": "Taiwan", "taipei": "Taiwan",
    # Turkey
    "turkey": "Turkey", "turkish": "Turkey", "土耳其": "Turkey", "istanbul": "Turkey",
    # Thailand
    "thailand": "Thailand", "thai": "Thailand", "泰国": "Thailand", "bangkok": "Thailand",
    # Malaysia
    "malaysia": "Malaysia", "malaysian": "Malaysia", "马来西亚": "Malaysia",
    "kuala lumpur": "Malaysia",
    # Indonesia
    "indonesia": "Indonesia", "indonesian": "Indonesia", "印尼": "Indonesia",
    "jakarta": "Indonesia",
    # Germany
    "germany": "Germany", "german": "Germany", "德国": "Germany",
    "deutschland": "Germany",
    # Italy
    "italy": "Italy", "italian": "Italy", "意大利": "Italy",
    # United States
    "usa": "United States", "u.s.": "United States", "u.s.a": "United States",
    "united states": "United States", "america": "United States",
    "american": "United States", "美国": "United States",
    # United Arab Emirates
    "uae": "United Arab Emirates", "u.a.e": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates", "dubai": "United Arab Emirates",
    "阿联酋": "United Arab Emirates",
    # Saudi Arabia
    "saudi arabia": "Saudi Arabia", "saudi": "Saudi Arabia",
    "沙特": "Saudi Arabia", "riyadh": "Saudi Arabia",
    # Australia
    "australia": "Australia", "australian": "Australia", "澳大利亚": "Australia",
    "sydney": "Australia", "melbourne": "Australia",
    # --- +12 metals-relevant additions ---
    # Brazil — iron ore #2 globally, big in copper / steel
    "brazil": "Brazil", "brazilian": "Brazil", "巴西": "Brazil",
    "sao paulo": "Brazil", "são paulo": "Brazil", "rio de janeiro": "Brazil",
    # Chile — #1 copper producer in the world
    # ("chile" alone is too generic — chili pepper, etc. — so we rely on
    #  city names + "chilean")
    "chilean": "Chile", "智利": "Chile",
    "santiago": "Chile", "antofagasta": "Chile",
    # Peru — #2 copper producer
    # ("peru" alone is too short for safe substring match)
    "peruvian": "Peru", "秘鲁": "Peru", "lima": "Peru",
    # Mexico
    "mexico": "Mexico", "mexican": "Mexico", "墨西哥": "Mexico",
    "monterrey": "Mexico", "guadalajara": "Mexico",
    # Canada
    "canada": "Canada", "canadian": "Canada", "加拿大": "Canada",
    "toronto": "Canada", "montreal": "Canada", "vancouver": "Canada",
    # Russia
    "russia": "Russia", "russian": "Russia", "俄罗斯": "Russia",
    "moscow": "Russia", "st petersburg": "Russia",
    # South Africa
    "south africa": "South Africa", "south african": "South Africa",
    "南非": "South Africa", "johannesburg": "South Africa",
    "cape town": "South Africa", "durban": "South Africa",
    # Egypt
    "egypt": "Egypt", "egyptian": "Egypt", "埃及": "Egypt",
    "cairo": "Egypt", "alexandria": "Egypt",
    # Spain
    "spain": "Spain", "spanish": "Spain", "西班牙": "Spain",
    "madrid": "Spain", "barcelona": "Spain",
    # Poland — NOT "polish" (collides with the "polished" finish keyword)
    "poland": "Poland", "波兰": "Poland",
    "warsaw": "Poland", "krakow": "Poland",
    # France
    "france": "France", "french": "France", "法国": "France",
    "paris": "France", "lyon": "France", "marseille": "France",
    # United Kingdom — NOT bare "uk" (matches "duke", "huk", etc.)
    # NOT bare "british" (collides with "British Columbia" → Canada)
    "united kingdom": "United Kingdom", "great britain": "United Kingdom",
    "u.k.": "United Kingdom", "england": "United Kingdom",
    "london": "United Kingdom", "manchester": "United Kingdom",
    "sheffield": "United Kingdom", "英国": "United Kingdom",
}

# Category keyword → canonical category. A category is the broad product family
# we use for price normalization and AI prompt context. Order in the dict does
# not matter; longer keywords are checked first inside the matcher.
_CATEGORY_KW: dict[str, str] = {
    # ACP family
    "acp": "acp",
    "aluminium composite panel": "acp",
    "aluminum composite panel": "acp",
    "alucobond": "acp",
    "alubond": "acp",
    "aludecor": "acp",
    "alstrong": "acp",
    "cladding panel": "acp",
    "复合板": "acp",
    "铝塑板": "acp",
    # Stainless steel — checked before plain "steel" via length sort
    "stainless steel": "stainless_steel",
    "stainless": "stainless_steel",
    "ss sheet": "stainless_steel",
    "不锈钢": "stainless_steel",
    "304": "stainless_steel",
    "316": "stainless_steel",
    # Aluminum (raw, sheet, plate, coil)
    "aluminium sheet": "aluminum",
    "aluminum sheet": "aluminum",
    "aluminium plate": "aluminum",
    "aluminum plate": "aluminum",
    "aluminium coil": "aluminum",
    "aluminum coil": "aluminum",
    "aluminium": "aluminum",
    "aluminum": "aluminum",
    "铝板": "aluminum",
    "铝卷": "aluminum",
    # Carbon / mild steel
    "carbon steel": "steel",
    "mild steel": "steel",
    "steel sheet": "steel",
    "steel plate": "steel",
    "steel coil": "steel",
    "钢板": "steel",
    "碳钢": "steel",
    "steel": "steel",
    # Copper
    "copper sheet": "copper",
    "copper plate": "copper",
    "copper coil": "copper",
    "copper": "copper",
    "铜板": "copper",
    "紫铜": "copper",
    # Brass
    "brass sheet": "brass",
    "brass": "brass",
    "黄铜": "brass",
    # Zinc
    "zinc sheet": "zinc",
    "zinc": "zinc",
    "锌板": "zinc",
    # Titanium
    "titanium sheet": "titanium",
    "titanium": "titanium",
    "钛板": "titanium",
    # Tube / pipe (shape-based, only used when no metal family matched)
    "tube": "tube",
    "pipe": "pipe",
    "管": "tube",
}

# Surface variant → canonical variant tag.
_VARIANT_KW: dict[str, str] = {
    "marble": "marble", "marbled": "marble", "stone": "marble", "石纹": "marble",
    "wooden": "wooden", "wood grain": "wooden", "wood-grain": "wooden",
    "wood": "wooden", "木纹": "wooden",
    "brushed": "brushed", "hairline": "brushed", "拉丝": "brushed",
    "mirror": "mirror", "polished": "mirror", "镜面": "mirror",
    "solid color": "solid", "solid": "solid", "纯色": "solid",
    "pvdf": "pvdf_coated", "pvdf coated": "pvdf_coated",
    "feve": "feve_coated", "feve coated": "feve_coated",
    "anodized": "anodized", "阳极氧化": "anodized",
    "galvanized": "galvanized", "galvanised": "galvanized", "镀锌": "galvanized",
}

# Specification regex patterns. Used both to extract a spec hint AND as
# strong signals that the user has a specific product in mind.
_SPEC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(6061|6063|7075|5052|5083|3003|1100)(?:-?[a-z]\d*)?\b", re.I),  # Al alloys
    re.compile(r"\b(304|304L|316|316L|321|430|201)\b", re.I),                       # SS grades
    re.compile(r"\b(A36|S235|S275|S355|Q235|Q345|SS400)\b", re.I),                  # Carbon steel
    re.compile(r"\bC1100\d*\b", re.I),                                              # Copper alloy
    re.compile(r"\b(B1|A2|A1)\s*(?:grade|class|fire|rated)?\b", re.I),              # Fire grades
    re.compile(r"\b(?:ASTM|EN|ISO|JIS|DIN|GB)\s*[\-/]?\s*\d+\b", re.I),             # Standards
    re.compile(r"\b\d+(?:\.\d+)?\s*mm\b", re.I),                                    # mm thickness
    re.compile(r"\b\d+(?:/\d+)?\s*(?:inch|in|\")\b", re.I),                         # inch thickness
]

# Price patterns for the regex layer. Captures (min, max?, currency?, unit?).
# We only need a coarse extraction here — full per-currency validation happens
# downstream in modules.cleaner.
_PRICE_RE = re.compile(
    r"(?P<cur>usd|aud|eur|cny|rmb|inr|\$|¥|€|₹|a\$)?\s*"
    r"(?P<lo>\d+(?:[.,]\d+)?)"
    r"(?:\s*[-–to]+\s*(?P<hi>\d+(?:[.,]\d+)?))?"
    r"\s*(?P<unit>/\s*(?:sqm|m2|m²|ton|t|mt|tonne|kg|piece|pc|meter|m|ft))?",
    re.I,
)

# Map raw currency tokens to canonical 3-letter codes.
_CUR_MAP: dict[str, str] = {
    "$": "USD", "usd": "USD",
    "a$": "AUD", "aud": "AUD",
    "€": "EUR", "eur": "EUR",
    "¥": "CNY", "cny": "CNY", "rmb": "CNY",
    "₹": "INR", "inr": "INR",
}

# Map raw unit tokens to canonical units.
_UNIT_MAP: dict[str, str] = {
    "sqm": "sqm", "m2": "sqm", "m²": "sqm",
    "ton": "ton", "t": "ton", "mt": "ton", "tonne": "ton",
    "kg": "kg",
    "piece": "piece", "pc": "piece",
    "meter": "meter", "m": "meter",
    "ft": "ft",
}


# ---------------------------------------------------------------------------
# LLM Layer 1
# ---------------------------------------------------------------------------

def _build_parse_prompt(user_msg: str) -> str:
    """
    Compact one-shot prompt. Two changes from a naive instruction prompt:
      1. We show a complete input/output example so Gemma mimics the
         JSON-only format instead of writing a Markdown reasoning trace.
      2. The prompt ends mid-JSON ("Output:") so the model continues from
         "{" rather than starting with prose like "Here is the JSON...".

    Without this, gemma-3-27b-it consistently emits a 1500-2000 char
    Markdown explanation before the JSON, blowing past the token cap and
    the LLM timeout.
    """
    return f"""Convert a metal-supplier search query to JSON. Output JSON only.

Example input: "6061 aluminum plate USD 3000-4000/ton from Vietnam"
Example output:
{{"category":"aluminum","material":"6061","variant":"","countries":["Vietnam"],"supplier_names":[],"price_range":{{"min":3000,"max":4000,"currency":"USD","unit":"ton"}},"spec":"","quantity":null,"needs_clarification":false,"clarification_question":""}}

Allowed values:
- category: acp|aluminum|steel|stainless_steel|copper|brass|zinc|titanium|tube|pipe|unknown
- variant: marble|wooden|brushed|mirror|solid|pvdf_coated|feve_coated|anodized|galvanized|""
- countries: canonical English names (e.g. China, India, South Korea, Vietnam, Turkey, United States)
- price_range.currency: USD|AUD|EUR|CNY|INR|null
- price_range.unit: sqm|ton|kg|meter|piece|ft|null

Set needs_clarification=true ONLY for genuinely vague input ("metal", "supplier", "cheap"); a bare product name like "ACP" is NOT vague.

Input: "{user_msg}"
Output:
"""


def _validate_schema(parsed: Any) -> bool:
    """Light schema check — just enough to confirm the LLM returned the right shape."""
    if not isinstance(parsed, dict):
        return False
    if not isinstance(parsed.get("category"), str):
        return False
    if not isinstance(parsed.get("countries", []), list):
        return False
    pr = parsed.get("price_range", {})
    if not isinstance(pr, dict):
        return False
    return True


def _normalize_llm_output(parsed: dict, raw_query: str) -> dict:
    """Coerce LLM output into the canonical schema with safe defaults."""
    pr = parsed.get("price_range") or {}
    qty = parsed.get("quantity")
    if qty is not None and not isinstance(qty, dict):
        qty = None

    return {
        "category":       (parsed.get("category") or "unknown").strip().lower(),
        "material":       (parsed.get("material") or "").strip(),
        "variant":        (parsed.get("variant") or "").strip().lower(),
        "countries":      [str(c).strip() for c in (parsed.get("countries") or []) if c],
        "supplier_names": [str(s).strip() for s in (parsed.get("supplier_names") or []) if s],
        "price_range": {
            "min":      _safe_float(pr.get("min")),
            "max":      _safe_float(pr.get("max")),
            "currency": (pr.get("currency") or None) and str(pr["currency"]).upper(),
            "unit":     (pr.get("unit")     or None) and str(pr["unit"]).lower(),
        },
        "spec":     (parsed.get("spec") or "").strip(),
        "quantity": qty,
        "raw_query": raw_query,
        "source":   "llm",
        "needs_clarification":   bool(parsed.get("needs_clarification", False)),
        "clarification_question": (parsed.get("clarification_question") or "").strip(),
    }


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Regex Layer 2 — deterministic keyword extraction
# ---------------------------------------------------------------------------

def fallback_parse(user_msg: str) -> dict:
    """
    Deterministic regex extractor used when the LLM is unavailable or returns
    invalid output. Will never raise — returns canonical schema with whatever
    it could extract.
    """
    text = user_msg or ""
    low  = text.lower()

    return {
        "category":       _detect_category(low),
        "material":       _detect_material(text),
        "variant":        _detect_variant(low),
        "countries":      _detect_countries(low),
        "supplier_names": [],
        "price_range":    _detect_price_range(text),
        "spec":           _detect_spec(text),
        "quantity":       None,
        "raw_query":      text,
        "source":         "regex",
        "needs_clarification":   False,
        "clarification_question": "",
    }


def _detect_category(low: str) -> str:
    """Match longest keyword first so 'stainless steel' beats 'steel'."""
    matches: list[tuple[int, str]] = []
    for kw, cat in _CATEGORY_KW.items():
        if kw in low:
            matches.append((len(kw), cat))
    if not matches:
        return "unknown"
    matches.sort(reverse=True)
    return matches[0][1]


def _detect_material(text: str) -> str:
    """Return the first spec-pattern hit (e.g. '6061', '304', 'A36')."""
    for pat in _SPEC_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return ""


def _detect_variant(low: str) -> str:
    for kw, var in _VARIANT_KW.items():
        if kw in low:
            return var
    return ""


def _detect_countries(low: str) -> list[str]:
    """Return unique canonical country names in first-mention order."""
    seen: list[str] = []
    for kw, canon in _COUNTRY_KW.items():
        if kw in low and canon not in seen:
            seen.append(canon)
    return seen


def _detect_price_range(text: str) -> dict:
    """
    Coarse price extractor for the regex layer. Looks for the first
    USD/AUD/EUR/CNY/INR amount or range. Returns None fields when nothing
    plausible is found.
    """
    blank = {"min": None, "max": None, "currency": None, "unit": None}
    if not text:
        return blank

    for m in _PRICE_RE.finditer(text):
        lo_raw = m.group("lo")
        hi_raw = m.group("hi")
        if not lo_raw:
            continue
        cur_raw  = (m.group("cur") or "").lower().strip()
        unit_raw = (m.group("unit") or "").lower().lstrip("/").strip()

        currency = _CUR_MAP.get(cur_raw) if cur_raw else None
        unit     = _UNIT_MAP.get(unit_raw) if unit_raw else None

        # Only accept matches that have at least currency OR unit context, to
        # avoid grabbing arbitrary numbers from the user message.
        if not (currency or unit):
            continue

        lo = _safe_float(lo_raw.replace(",", ""))
        hi = _safe_float(hi_raw.replace(",", "")) if hi_raw else None
        return {"min": lo, "max": hi, "currency": currency, "unit": unit}

    return blank


def _detect_spec(text: str) -> str:
    """Concatenate any spec-pattern hits into a short reference string."""
    hits: list[str] = []
    for pat in _SPEC_PATTERNS:
        for m in pat.finditer(text):
            tok = m.group(0).strip()
            if tok and tok not in hits:
                hits.append(tok)
    return ", ".join(hits)


# ---------------------------------------------------------------------------
# Public API: regex-first, LLM-on-demand
# ---------------------------------------------------------------------------

def _is_confident(parsed: dict) -> bool:
    """
    Decide whether a regex parse is strong enough to skip the LLM.

    "Confident" means we extracted a real metal family AND at least one
    other anchor: a country, a material/grade (e.g. 6061, 304), or a spec.
    Any one of those, combined with a known category, is enough to drive
    a useful multi-angle search.

    This is the gate that turns the LLM from a blocking dependency into a
    conditional helper: structured queries skip Gemma entirely (~0ms);
    only vague queries pay the LLM latency to gain natural-language
    understanding and the `needs_clarification` guardrail.
    """
    cat = parsed.get("category")
    if not cat or cat == "unknown":
        return False
    if parsed.get("countries"):
        return True
    if parsed.get("material") or parsed.get("spec"):
        return True
    return False


def parse_search_query(user_msg: str) -> dict:
    """
    Parse a user's natural-language search request.

    Strategy (regex-first, LLM-on-demand):
      1. Regex always runs. If the result is "confident" (see _is_confident)
         we return it immediately. Typical latency: ~0ms.
      2. Otherwise we call the LLM with a hard 1s cap and no retries.
         If it answers, we use it; if not, we keep the regex result.
      3. If the LLM is slow or unavailable, fall back to whatever the
         regex produced rather than blocking.

    The LLM is an enhancement, never a hard dependency: when Gemma is
    down (or, today, when Gemma is up but ignoring our JSON instructions),
    search still works.

    TODO: switch model to gemini-flash for the parse path. gemma-3-27b-it
    is structurally trained to emit a 1500-1800 char Markdown reasoning
    preamble before any JSON, regardless of `responseMimeType` or prompt
    instructions, so it never finishes within a sane parse budget. With
    gemini-flash + json_mode, the LLM branch will start actually firing
    on vague queries (~1-2s) and the 1s timeout below should be raised.
    """
    user_msg = (user_msg or "").strip()
    if not user_msg:
        return _empty_result("")

    # --- Layer 1: regex (always runs, fast, deterministic) ----------------
    regex_result = fallback_parse(user_msg)
    if _is_confident(regex_result):
        return regex_result

    # --- Layer 2: LLM (only when regex is not confident enough) -----------
    # Tight 1s cap because the current model (gemma-3-27b-it) almost never
    # responds with usable JSON within a sane budget — see TODO above.
    # Keeping the call path here so a future model swap (gemini-flash) is
    # a one-line config change; until then, vague queries fall through to
    # the regex result without making the user wait.
    try:
        response = call_model_fast(
            _build_parse_prompt(user_msg),
            timeout=1.0,
            json_mode=True,    # required by gemini-* models, no-op on gemma-*
        )
        if response:
            parsed = _extract_json(response)
            if _validate_schema(parsed):
                return _normalize_llm_output(parsed, user_msg)
            _log.warning("[query_parser] LLM output failed schema check")
    except Exception as e:  # noqa: BLE001
        _log.warning("[query_parser] LLM parse failed: %s", e)

    # --- Layer 3: regex result (even if not confident) or raw -------------
    if regex_result["category"] != "unknown" or regex_result["countries"]:
        return regex_result
    return _empty_result(user_msg, source="raw")


def _empty_result(user_msg: str, source: str = "raw") -> dict:
    """Shape used by Layer 3 and for empty input."""
    return {
        "category":       "unknown",
        "material":       "",
        "variant":        "",
        "countries":      [],
        "supplier_names": [],
        "price_range":    {"min": None, "max": None, "currency": None, "unit": None},
        "spec":           "",
        "quantity":       None,
        "raw_query":      user_msg,
        "source":         source,
        "needs_clarification":   False,
        "clarification_question": "",
    }
