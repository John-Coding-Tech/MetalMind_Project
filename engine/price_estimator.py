"""
engine/price_estimator.py

Per-supplier price estimation (the "model" half of the hybrid pricing system).

Philosophy — locked by product review:
  1. Real extracted prices always win. The estimator is ONLY consulted for
     suppliers whose page didn't publish a number.
  2. Estimates are emitted as asymmetric RANGES, never single points.
  3. No training data — the model is a transparent multiplicative composition
     of signals we already scrape:
         base × country × supplier_type × variant × scale_discount
  4. Every output is tagged `source="model"` so the UI can render it with
     clear "⚠ model" visual treatment, demoted vs real prices.

Public API:
    estimate_supplier_price(record, category, query_variant) -> dict

Output:
    {
      "point":   float,   # central USD / canonical-unit
      "low":     float,   # -20% of point
      "high":    float,   # +30% of point
      "unit":    str,     # canonical unit used for base lookup
      "source":  "model",
    }
    or None when the category has no reference in config (unknown metal).

    market_reference_for(category, countries) -> dict
    -> {"low_usd", "high_usd", "unit", "countries"} or None
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from modules.cleaner import SupplierRecord
from config import (
    CATEGORY_MEDIANS_USD,
    COUNTRY_PRICE_MULTIPLIER,
    SUPPLIER_TYPE_MULTIPLIER,
    VARIANT_PRICE_MULTIPLIER,
    MARKET_RANGE_THRESHOLDS,
    ESTIMATE_RANGE_LOW_MULT,
    ESTIMATE_RANGE_HIGH_MULT,
)

_log = logging.getLogger(__name__)


# Map category -> the canonical unit we price it in. Mirrors
# modules.cleaner._CATEGORY_DEFAULT_UNIT but kept local so this module has
# no reverse dependency on the cleaner.
_CATEGORY_UNIT: dict[str, str] = {
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
}


# ---------------------------------------------------------------------------
# Scale discount — treat "large-scale operation" hints as 2-5% discounts.
# Capped at -10% total so it can't flip pricing too much on its own.
# ---------------------------------------------------------------------------

def _scale_discount(scale_hint: dict | None) -> float:
    """
    Return a multiplicative factor in [0.90, 1.00]. Each strong scale hit
    shaves 3-5% off; multiple hits stack but the total is clamped at -10%.
    """
    if not scale_hint:
        return 1.00
    mult = 1.0
    if scale_hint.get("workers", 0) >= 500:
        mult *= 0.96
    if scale_hint.get("area_sqm", 0) >= 20_000:
        mult *= 0.95
    if scale_hint.get("annual_ton", 0) >= 10_000:
        mult *= 0.95
    if scale_hint.get("daily_sqm", 0) >= 5_000:
        mult *= 0.97
    return max(0.90, mult)


# ---------------------------------------------------------------------------
# Public estimator
# ---------------------------------------------------------------------------

def estimate_supplier_price(
    record: SupplierRecord,
    category: str,
    query_variant: str = "",
) -> dict | None:
    """
    Compose a per-supplier estimate using only signals already on the
    record + the user's search context. Returns None when the category
    has no config reference (e.g. `category="unknown"`).
    """
    cat = category or "unknown"
    unit = _CATEGORY_UNIT.get(cat)
    if not unit:
        return None

    base = CATEGORY_MEDIANS_USD.get((cat, unit))
    if not base:
        return None

    country_mult = COUNTRY_PRICE_MULTIPLIER.get(record.country, 1.00)
    type_mult    = SUPPLIER_TYPE_MULTIPLIER.get(record.supplier_type or "unknown", 1.05)

    # Variant from the query wins (all suppliers in the same search share a
    # variant hint); fall back to empty, which maps to 1.00.
    variant_mult = VARIANT_PRICE_MULTIPLIER.get((query_variant or "").lower(), 1.00)

    scale_hint   = {}
    if record.signals and isinstance(record.signals, dict):
        scale_hint = record.signals.get("scale_hint") or {}
    scale_mult   = _scale_discount(scale_hint)

    point = base * country_mult * type_mult * variant_mult * scale_mult
    point = round(point, 4)

    low  = round(point * ESTIMATE_RANGE_LOW_MULT,  4)
    high = round(point * ESTIMATE_RANGE_HIGH_MULT, 4)

    return {
        "point":  point,
        "low":    low,
        "high":   high,
        "unit":   unit,
        "source": "model",
    }


# ---------------------------------------------------------------------------
# Range classification — bucket a real price against the per-country market
# midpoint. Returns the string the frontend uses to pick a badge.
# ---------------------------------------------------------------------------

def classify_price_vs_market(
    price_usd: float | None,
    category: str,
    country: str,
    query_variant: str = "",
) -> str | None:
    """
    Returns one of: "suspicious_low" | "within" | "above" | "far_above",
    or None when we can't form a market midpoint for this supplier.
    """
    if price_usd is None or price_usd <= 0:
        return None
    cat  = category or "unknown"
    unit = _CATEGORY_UNIT.get(cat)
    if not unit:
        return None
    base = CATEGORY_MEDIANS_USD.get((cat, unit))
    if not base:
        return None

    midpoint = (
        base
        * COUNTRY_PRICE_MULTIPLIER.get(country, 1.00)
        * VARIANT_PRICE_MULTIPLIER.get((query_variant or "").lower(), 1.00)
    )
    if midpoint <= 0:
        return None

    ratio = price_usd / midpoint
    if ratio < MARKET_RANGE_THRESHOLDS["suspicious_below"]:
        return "suspicious_low"
    if ratio < MARKET_RANGE_THRESHOLDS["within_upper"]:
        return "within"
    if ratio < MARKET_RANGE_THRESHOLDS["above_upper"]:
        return "above"
    return "far_above"


# ---------------------------------------------------------------------------
# Market reference band for the page-wide banner. Considers the user's
# specified countries (if any) to produce a country-scoped band.
# ---------------------------------------------------------------------------

def market_reference_for(
    category: str,
    countries: list[str] | None = None,
    query_variant: str = "",
) -> dict | None:
    """
    Compute the top-of-page market reference band in USD per canonical unit.

    Band width = the outer envelope of the per-country adjusted midpoint
    combined with the same asymmetric range (LOW_MULT / HIGH_MULT) we use
    for per-supplier estimates — so the banner and individual rows stay
    numerically consistent.

    Returns None when the category has no config reference (frontend hides
    the banner in that case).
    """
    cat  = category or "unknown"
    unit = _CATEGORY_UNIT.get(cat)
    if not unit:
        return None
    base = CATEGORY_MEDIANS_USD.get((cat, unit))
    if not base:
        return None

    variant_mult = VARIANT_PRICE_MULTIPLIER.get((query_variant or "").lower(), 1.00)

    if countries:
        mids = [
            base * COUNTRY_PRICE_MULTIPLIER.get(c, 1.00) * variant_mult
            for c in countries
        ]
        country_scope = list(countries)
    else:
        mids = [base * variant_mult]
        country_scope = []   # global band

    band_lo = min(mids) * ESTIMATE_RANGE_LOW_MULT
    band_hi = max(mids) * ESTIMATE_RANGE_HIGH_MULT

    return {
        "low_usd":       round(band_lo, 4),
        "high_usd":      round(band_hi, 4),
        "unit":          unit,
        "category":      cat,
        "country_scope": country_scope,
    }
