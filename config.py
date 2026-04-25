"""
config.py — Centralized tuning constants.

Single source of truth for all decision thresholds and scoring coefficients.
Previously these were duplicated across main.py, value_scorer.py,
comparator.py, and ai_engine.py; drift between copies caused subtle bugs.

ALL tunable numbers belong here.
"""

# ---------------------------------------------------------------------------
# Decision thresholds (0-1 scale)
# ---------------------------------------------------------------------------

# A supplier is "recommended" when value_score >= this threshold
# AND risk_level is not HIGH.
DECISION_SCORE_THRESHOLD: float = 0.5

# Risk-level cutoffs — applied uniformly across rule-based, AI-only,
# and AI-comparison pipelines so the same supplier never gets different
# risk labels in different modes.
RISK_LEVEL_LOW_MAX:    float = 0.25   # risk_score < LOW_MAX  → Low
RISK_LEVEL_MEDIUM_MAX: float = 0.55   # risk_score < MEDIUM_MAX → Medium, else High


# ---------------------------------------------------------------------------
# Value-scoring coefficients
# ---------------------------------------------------------------------------

# Formula: value_score = price_score * (1 - RISK_WEIGHT * risk_score)
RISK_WEIGHT: float = 0.6

# Soft-cap zone for high-risk suppliers — value smoothly decays toward
# HIGH_RISK_VALUE_CAP as risk_score moves from DECAY_START to DECAY_END.
# Previously this was a hard step at 0.7 causing discontinuous ranks.
HIGH_RISK_DECAY_START: float = 0.60
HIGH_RISK_DECAY_END:   float = 0.80
HIGH_RISK_VALUE_CAP:   float = 0.30


# ---------------------------------------------------------------------------
# AI vs Expert reconciliation
# ---------------------------------------------------------------------------

# If AI's risk_score exceeds expert's by more than this, flag tier as yellow
AI_RISK_ESCALATION: float = 0.25


# ---------------------------------------------------------------------------
# Pipeline sizing (can also be overridden via env vars in main.py)
# ---------------------------------------------------------------------------

AI_COMPARE_TOP_N_DEFAULT: int = 3
AI_ONLY_TOP_N_DEFAULT:    int = 3
AI_MAX_PARALLEL_DEFAULT:  int = 5


# ---------------------------------------------------------------------------
# Multi-metal / multi-unit category economics
#
# CATEGORY_MEDIANS_USD: typical wholesale price per unit for each
# (category, unit) bucket, in USD. Used by value_scorer as the *fallback
# normalization anchor* when fewer than MIN_BUCKET_SAMPLES suppliers in the
# dataset share the same bucket. Numbers are deliberately ballpark — the
# median doesn't need precision, it just needs to put a $5/sqm ACP and a
# $25/sqm ACP on opposite sides of "typical".
# ---------------------------------------------------------------------------

# (category, unit) -> typical median USD price
CATEGORY_MEDIANS_USD: dict[tuple[str, str], float] = {
    # Aluminium composite panels
    ("acp",             "sqm"):   20.0,

    # Raw metals — per-ton wholesale order of magnitude
    ("aluminum",        "ton"):   3000.0,
    ("aluminum",        "kg"):    3.0,
    ("steel",           "ton"):   800.0,
    ("steel",           "kg"):    0.8,
    ("stainless_steel", "ton"):   3000.0,
    ("stainless_steel", "kg"):    3.0,
    ("copper",          "ton"):   9000.0,
    ("copper",          "kg"):    9.0,
    ("brass",           "ton"):   7000.0,
    ("brass",           "kg"):    7.0,
    ("zinc",            "ton"):   2800.0,
    ("zinc",            "kg"):    2.8,
    ("titanium",        "kg"):    50.0,
    ("titanium",        "ton"):   50000.0,

    # Shape-based products
    ("tube",            "meter"): 10.0,
    ("pipe",            "meter"): 15.0,
}

# Unit -> physical dimension class. Conversion is only allowed *within* the
# same dimension; cross-dimension comparison is meaningless ("3000 kg" cannot
# be compared to "10 meters").
UNIT_DIMENSION: dict[str, str] = {
    "sqm":     "area",
    "ton":     "mass",
    "kg":      "mass",
    "meter":   "length",
    "ft":      "length",
    "piece":   "count",
    "unknown": "unknown",
}

# Within-dimension conversion ratios. Key: (from_unit, to_unit) -> multiplier.
# Used by value_scorer to bring all prices in a bucket into a single canonical
# unit before normalization (e.g. when the bucket mixes /ton and /kg quotes).
UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    # Mass — canonical = kg
    ("ton", "kg"):    1000.0,
    ("kg",  "ton"):   0.001,
    # Length — canonical = meter
    ("ft",  "meter"): 0.3048,
    ("meter", "ft"):  3.28084,
    # Identity (helps callers avoid special-casing)
    ("sqm",   "sqm"):   1.0,
    ("ton",   "ton"):   1.0,
    ("kg",    "kg"):    1.0,
    ("meter", "meter"): 1.0,
    ("ft",    "ft"):    1.0,
    ("piece", "piece"): 1.0,
}

# Canonical unit per dimension — what value_scorer reduces every bucket to.
DIMENSION_CANONICAL_UNIT: dict[str, str] = {
    "area":   "sqm",
    "mass":   "kg",
    "length": "meter",
    "count":  "piece",
}

# Minimum sample size in a (category, unit) bucket before we trust live
# normalization. Below this we anchor to CATEGORY_MEDIANS_USD instead so a
# tiny dataset doesn't produce nonsense relative scores.
MIN_BUCKET_SAMPLES: int = 3


# ---------------------------------------------------------------------------
# Pipeline budgets
# ---------------------------------------------------------------------------

# Hard ceiling on total wall-clock time for a single /api/analyze request.
# Multi-search + clean + score + AI insight together must finish under this.
ANALYZE_TOTAL_BUDGET: float = 20.0   # seconds

# Display cap for the "All Suppliers Found" list. Ranking is computed over
# the full scraped set (for stable medians / anomaly baselines), but only
# the top-N after re-ranking are surfaced to the frontend. Keeps the page
# scannable — a 34-row list is noise after row 10 or so.
DISPLAY_SUPPLIER_LIMIT: int = 10


# ---------------------------------------------------------------------------
# File upload limits
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024   # 25 MB per file

ALLOWED_UPLOAD_EXTS: frozenset[str] = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
})


# ---------------------------------------------------------------------------
# Missing-price handling (value_scorer)
#
# When a supplier has NO extracted price, value_scorer has to assign *some*
# price_score to feed into the formula. The old behavior was "neutral 0.5"
# which was numerically honest but produced UX-bad outputs: a Low-risk
# supplier with no published price would cap at ~45/100 even as the top
# pick, making the system look unsure of itself.
#
# "Half reward": 0.75 is halfway between neutral (0.5) and cheapest (1.0).
# Interpretation: "we can't prove you're cheap, but in a market where most
# peers are also price-opaque, we won't penalize you at the level we would
# penalize a confirmed-expensive supplier either." Low-risk unpriced
# suppliers now land around 66/100, matching intuition that "Top 1 + Low
# risk" should feel solid.
# ---------------------------------------------------------------------------

MISSING_PRICE_SCORE: float = 0.75


# ---------------------------------------------------------------------------
# Per-supplier price estimation (hybrid pricing system, "path C")
#
# Philosophy (locked by product review):
#   1. Real extracted prices always win — estimates only show when no real
#      price is available.
#   2. Estimates are *ranges*, never single points ("A$10–17", not "A$13").
#   3. Estimates are visually secondary (grey, small, labeled "⚠ model").
#   4. The market reference banner is the macro context; per-supplier
#      estimates are the micro context; neither replaces RFQ.
#
# The estimator composes signals we already scrape, so no training data is
# needed — just sensible multipliers grounded in B2B pricing intuition:
#
#     point_estimate =
#         CATEGORY_MEDIANS_USD[(category, unit)]   (base)
#         × COUNTRY_PRICE_MULTIPLIER[country]      (regional labor / tariffs)
#         × SUPPLIER_TYPE_MULTIPLIER[type]         (factory vs trader markup)
#         × VARIANT_PRICE_MULTIPLIER[variant]      (coating / finish premium)
#         × scale_discount (if page shows large factory hints)
#
# Output range: [point × ESTIMATE_RANGE_LOW, point × ESTIMATE_RANGE_HIGH]
# ---------------------------------------------------------------------------

# Country multiplier vs global median. 1.0 = global average, <1 = cheaper,
# >1 = pricier. Based on typical labor + logistics + regulatory cost tiers.
COUNTRY_PRICE_MULTIPLIER: dict[str, float] = {
    "China":                0.85,
    "India":                0.65,
    "Vietnam":              0.75,
    "South Korea":          1.05,
    "Japan":                1.20,
    "Taiwan":               1.00,
    "Turkey":               1.00,
    "Thailand":             0.90,
    "Malaysia":             0.95,
    "Indonesia":            0.85,
    "Germany":              1.30,
    "Italy":                1.30,
    "United States":        1.25,
    "United Arab Emirates": 1.10,
    "Saudi Arabia":         1.10,
    "Australia":            1.15,
    # "Unknown" falls through to 1.0 (caller uses .get(country, 1.0))
}

# Supplier-type markup. Manufacturer = direct, no middleman. Trader /
# reseller add margin. Unknown is a slight markup because the uncertainty
# leans toward "middleman" in practice.
SUPPLIER_TYPE_MULTIPLIER: dict[str, float] = {
    "manufacturer": 1.00,
    "trader":       1.15,
    "reseller":     1.10,
    "unknown":      1.05,
}

# Variant / finish premium. ACP-centric but applies generically (wood-grain,
# mirror, etc. cost more across metal categories). "solid" = no premium.
VARIANT_PRICE_MULTIPLIER: dict[str, float] = {
    "marble":       1.15,
    "wooden":       1.15,
    "brushed":      1.10,
    "mirror":       1.20,
    "solid":        1.00,
    "pvdf_coated":  1.30,
    "feve_coated":  1.35,
    "anodized":     1.20,
    "galvanized":   1.05,
    "":             1.00,
}

# Range classification for a supplier's real (scraped) price vs its
# per-country-adjusted market midpoint. User-locked thresholds — tuned to
# reduce false "above market" flags on legitimately high-end products.
#
#   ratio < 0.60           -> suspicious_low
#   0.60 <= ratio < 1.60   -> within
#   1.60 <= ratio < 2.20   -> above
#   ratio >= 2.20          -> far_above
MARKET_RANGE_THRESHOLDS: dict[str, float] = {
    "suspicious_below": 0.60,
    "within_upper":     1.60,
    "above_upper":      2.20,
}

# Asymmetric range around the point estimate. -20% / +30% reflects that
# real metal pricing is right-skewed (small downside from base, bigger
# upside for high-spec variants).
ESTIMATE_RANGE_LOW_MULT:  float = 0.80
ESTIMATE_RANGE_HIGH_MULT: float = 1.30
