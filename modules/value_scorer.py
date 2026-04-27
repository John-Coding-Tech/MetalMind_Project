"""
modules/value_scorer.py

Value scoring module — multi-metal, multi-unit aware.

Formula (from rules/scoring_rules.md):
    value_score = price_score * (1 - RISK_WEIGHT * risk_score)

Where:
    price_score = normalised inverse price (higher = better value)
    RISK_WEIGHT = 0.6 (fixed, from config)

Multi-metal normalization (NEW):
    Suppliers are bucketed by (category, unit) and normalised *within* each
    bucket. This way ACP/sqm and steel/ton don't get jammed onto the same
    scale (which would put a $20/sqm panel and a $3000/ton coil at opposite
    ends of "expensive vs cheap" for nonsensical reasons).

    Within a bucket:
      - If we have >= MIN_BUCKET_SAMPLES suppliers with known prices, we use
        live min/max normalization across them.
      - Otherwise we anchor to CATEGORY_MEDIANS_USD with a +/-50% spread, so
        a single supplier in a bucket still gets a sensible price_score
        relative to the typical market median.

    Within-dimension unit conversion (e.g. /ton vs /kg) is applied *before*
    bucketing so all members of a (category, mass) bucket are compared in kg.

High-risk soft-cap:
    Between HIGH_RISK_DECAY_START and HIGH_RISK_DECAY_END the value is
    linearly blended toward HIGH_RISK_VALUE_CAP. Beyond DECAY_END it is
    fully capped. This replaces the old hard step at risk=0.7 which
    caused a ~0.28 discontinuous drop in the rankings.

No-price fallback:
    Suppliers without a scraped price receive config.MISSING_PRICE_SCORE
    (currently 0.75 = "half reward") rather than a strict neutral 0.5.
    Reasoning: in opaque B2B markets like ACP, most suppliers don't publish
    prices at all — penalizing them to mid-scale makes the Top 1 look
    underwhelming (~45/100 even for Low-risk picks). 0.75 nudges these
    toward "plausible good pick" territory (~66/100 at Low risk) without
    rewarding them as much as a confirmed-cheap supplier would be (~88/100).
"""

from dataclasses import dataclass

from modules.risk_scorer import ScoredSupplier
from config import (
    RISK_WEIGHT,
    HIGH_RISK_DECAY_START,
    HIGH_RISK_DECAY_END,
    HIGH_RISK_VALUE_CAP,
    CATEGORY_MEDIANS_USD,
    UNIT_DIMENSION,
    UNIT_CONVERSIONS,
    DIMENSION_CANONICAL_UNIT,
    MIN_BUCKET_SAMPLES,
    MISSING_PRICE_SCORE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ValuedSupplier:
    scored:       ScoredSupplier
    price_used:   float    # price used in calc, in canonical unit (USD/canonical-unit); 0.0 when unknown
    price_score:  float    # normalised price score (0-1, higher = cheaper = better)
    value_score:  float    # final combined score (0-1, higher = better)
    bucket_key:   str = "unknown:unknown"  # "category:canonical_unit", surfaced for debug
    bucket_size:  int = 0                  # # of priced suppliers in the bucket; <MIN_BUCKET_SAMPLES means anchored to median


# ---------------------------------------------------------------------------
# Bucketing helpers
# ---------------------------------------------------------------------------

def _bucket_for(category: str, unit: str) -> tuple[str, str]:
    """
    Map a supplier's (category, unit) to its canonical bucket key.
    The unit is reduced to the dimension's canonical unit (kg for mass,
    meter for length, sqm for area) so "ton" and "kg" land in the same
    bucket and are compared on the same scale.
    """
    cat = category or "unknown"
    u   = unit or "unknown"
    dim = UNIT_DIMENSION.get(u, "unknown")
    if dim == "unknown":
        return (cat, u)
    canonical = DIMENSION_CANONICAL_UNIT.get(dim, u)
    return (cat, canonical)


def _convert_price(price: float, from_unit: str, to_unit: str) -> float | None:
    """
    Convert a price quoted in `from_unit` to `to_unit`, only valid within the
    same dimension. Returns None if the conversion is unknown or undefined.

    Example: $3000/ton -> $3/kg (multiplier 0.001).
    """
    if from_unit == to_unit:
        return price
    if UNIT_DIMENSION.get(from_unit) != UNIT_DIMENSION.get(to_unit):
        return None
    # Price per unit conversion is the *inverse* of the quantity conversion:
    # if 1 ton = 1000 kg, then $3000/ton = $3000 / 1000 = $3/kg.
    qty_mult = UNIT_CONVERSIONS.get((from_unit, to_unit))
    if qty_mult is None or qty_mult == 0:
        return None
    return price / qty_mult


def _median_anchor(category: str, canonical_unit: str) -> float | None:
    """
    Return the typical USD median price for a (category, canonical_unit)
    bucket from config.CATEGORY_MEDIANS_USD. The lookup tries the canonical
    unit first; if that fails, it tries common alternates within the same
    dimension (e.g. "kg" -> try "ton" and convert).
    """
    direct = CATEGORY_MEDIANS_USD.get((category, canonical_unit))
    if direct is not None:
        return direct

    dim = UNIT_DIMENSION.get(canonical_unit, "unknown")
    if dim == "unknown":
        return None

    # Walk other units of the same dimension and convert into canonical_unit.
    for (cat_key, u_key), median in CATEGORY_MEDIANS_USD.items():
        if cat_key != category:
            continue
        if UNIT_DIMENSION.get(u_key) != dim:
            continue
        converted = _convert_price(median, u_key, canonical_unit)
        if converted is not None:
            return converted
    return None


# ---------------------------------------------------------------------------
# Per-bucket normalization
# ---------------------------------------------------------------------------

def _normalise_within_bucket(
    prices: list[float],
    bucket_key: tuple[str, str],
) -> list[float]:
    """
    Compute price_scores for one bucket. Strategy depends on sample size:

      - >= MIN_BUCKET_SAMPLES: live min/max normalization within the bucket.
      - <  MIN_BUCKET_SAMPLES but median anchor exists: normalize against
        median +/- 50% spread so even a single supplier gets a meaningful
        score relative to typical market pricing.
      - Otherwise: neutral 0.5 (no info).

    Higher score = cheaper = better.
    """
    n = len(prices)
    if n == 0:
        return []

    if n >= MIN_BUCKET_SAMPLES:
        lo, hi = min(prices), max(prices)
        if hi == lo:
            return [1.0] * n
        return [round(1.0 - (p - lo) / (hi - lo), 4) for p in prices]

    cat, canon_unit = bucket_key
    median = _median_anchor(cat, canon_unit)
    if median is None or median <= 0:
        return [0.5] * n

    # Anchor to median +/-50%. Within that band: cheaper -> higher score.
    lo = median * 0.5
    hi = median * 1.5
    out: list[float] = []
    for p in prices:
        if p <= lo:
            out.append(1.0)
        elif p >= hi:
            out.append(0.0)
        else:
            out.append(round(1.0 - (p - lo) / (hi - lo), 4))
    return out


# ---------------------------------------------------------------------------
# Soft-cap
# ---------------------------------------------------------------------------

def _apply_high_risk_decay(value: float, risk_score: float) -> float:
    """
    Linearly blend `value` toward HIGH_RISK_VALUE_CAP as risk_score goes
    from DECAY_START to DECAY_END. Beyond DECAY_END: fully capped.
    """
    if risk_score <= HIGH_RISK_DECAY_START:
        return value
    if risk_score >= HIGH_RISK_DECAY_END:
        return min(value, HIGH_RISK_VALUE_CAP)

    span   = HIGH_RISK_DECAY_END - HIGH_RISK_DECAY_START
    weight = (risk_score - HIGH_RISK_DECAY_START) / span
    capped = min(value, HIGH_RISK_VALUE_CAP)
    return value * (1.0 - weight) + capped * weight


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_value_scores(
    scored_suppliers: list[ScoredSupplier],
    query_variant: str = "",
) -> list[ValuedSupplier]:
    """
    Compute value scores for all suppliers, bucketed by (category, canonical_unit).

    Args:
        scored_suppliers: Output of risk_scorer.score_all()
        query_variant:    Optional variant from the parsed query (e.g.
                          "marble", "pvdf_coated"). Used to classify each
                          extracted price against the per-country market
                          midpoint — prices that classify as
                          "suspicious_low" are demoted to MISSING_PRICE_SCORE
                          so a regex-extracted-but-implausibly-cheap number
                          can't outrank a real Low-risk no-price supplier
                          (Bug B fix).

    Returns:
        List of ValuedSupplier in the same order as the input, each annotated
        with bucket_key + bucket_size for downstream debug/UI.
    """
    # Local import to avoid a circular dep (price_estimator imports nothing
    # from this module, but the engine ↔ modules boundary stays one-way).
    from engine.price_estimator import classify_price_vs_market
    n = len(scored_suppliers)
    if n == 0:
        return []

    # === Pass 1: bucket every supplier and convert prices to canonical unit ===
    buckets:    dict[tuple[str, str], list[int]]    = {}
    canon_price: list[float | None] = [None] * n
    bucket_keys: list[tuple[str, str]] = [("unknown", "unknown")] * n

    for i, s in enumerate(scored_suppliers):
        rec  = s.record
        cat  = getattr(rec, "category",   "unknown") or "unknown"
        unit = getattr(rec, "price_unit", "unknown") or "unknown"
        key  = _bucket_for(cat, unit)
        bucket_keys[i] = key
        buckets.setdefault(key, []).append(i)

        if rec.price_est is None:
            continue

        # Reduce raw quote into the bucket's canonical unit so all members
        # of (steel, mass) land in $/kg regardless of original /ton vs /kg.
        canon_unit = key[1]
        converted  = _convert_price(rec.price_est, unit, canon_unit)
        canon_price[i] = converted if converted is not None else rec.price_est

    # === Global no-price short-circuit =====================================
    # Every supplier in the dataset is price-less. Use the MISSING_PRICE_SCORE
    # "half reward" so the Top 1 doesn't land at ~45/100 on a Low-risk match
    # (see config for rationale).
    has_any_price = any(p is not None for p in canon_price)
    if not has_any_price:
        return [
            ValuedSupplier(
                scored=s,
                price_used=0.0,
                price_score=MISSING_PRICE_SCORE,
                value_score=round(_apply_high_risk_decay(
                    MISSING_PRICE_SCORE * (1.0 - RISK_WEIGHT * s.risk_score),
                    s.risk_score), 4),
                bucket_key=f"{bucket_keys[i][0]}:{bucket_keys[i][1]}",
                bucket_size=0,
            )
            for i, s in enumerate(scored_suppliers)
        ]

    # === Pass 2: per-bucket normalization ==================================
    price_score: list[float] = [MISSING_PRICE_SCORE] * n
    price_used:  list[float] = [0.0] * n
    bucket_size: list[int]   = [0]   * n

    for key, idxs in buckets.items():
        # Bug B fix: filter out prices classified as "suspicious_low"
        # against the per-country market midpoint. They get the missing-
        # price treatment so a $4500/ton (50% of $9000 copper market)
        # doesn't outrank a real Low-risk no-price supplier just by
        # virtue of being cheapest in the dataset.
        priced_idxs = []
        suspicious_idxs = []
        for i in idxs:
            if canon_price[i] is None:
                continue
            rec_i = scored_suppliers[i].record
            cls = classify_price_vs_market(
                rec_i.price_est,                              # original-unit price
                getattr(rec_i, "category", "") or "unknown",
                rec_i.country,
                query_variant,
            )
            if cls == "suspicious_low":
                suspicious_idxs.append(i)
            else:
                priced_idxs.append(i)

        priced_values = [canon_price[i] for i in priced_idxs]
        scores = _normalise_within_bucket(priced_values, key)
        for i, sc in zip(priced_idxs, scores):
            price_score[i] = sc
            price_used[i]  = canon_price[i]
            bucket_size[i] = len(priced_values)

        # Suspicious-low priced suppliers: same treatment as no-price
        # (MISSING_PRICE_SCORE), but we keep their numeric price_used so
        # the UI badge ("🚨 Suspiciously low") still appears next to it.
        for i in suspicious_idxs:
            price_score[i] = MISSING_PRICE_SCORE
            price_used[i]  = canon_price[i]
            bucket_size[i] = len(priced_values)

        # Suppliers in this bucket WITHOUT a price get the "half reward"
        # fallback — same value the global no-price branch uses above.
        for i in idxs:
            if canon_price[i] is None:
                price_score[i] = MISSING_PRICE_SCORE
                bucket_size[i] = len(priced_values)

    # === Pass 3: combine with risk + soft-cap ==============================
    out: list[ValuedSupplier] = []
    for i, s in enumerate(scored_suppliers):
        value = price_score[i] * (1.0 - RISK_WEIGHT * s.risk_score)
        value = round(_apply_high_risk_decay(value, s.risk_score), 4)

        out.append(ValuedSupplier(
            scored=s,
            price_used=price_used[i],
            price_score=price_score[i],
            value_score=value,
            bucket_key=f"{bucket_keys[i][0]}:{bucket_keys[i][1]}",
            bucket_size=bucket_size[i],
        ))
    return out
