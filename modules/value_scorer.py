"""
modules/value_scorer.py

Value scoring module.

Formula (from rules/scoring_rules.md):
    value_score = price_score * (1 - RISK_WEIGHT * risk_score)

Where:
    price_score = normalised inverse price (higher = better value)
    RISK_WEIGHT = 0.6 (fixed, from config)

High-risk soft-cap:
    Between HIGH_RISK_DECAY_START and HIGH_RISK_DECAY_END the value is
    linearly blended toward HIGH_RISK_VALUE_CAP. Beyond DECAY_END it is
    fully capped. This replaces the old hard step at risk=0.7 which
    caused a ~0.28 discontinuous drop in the rankings.

No-price fallback:
    If NO supplier in the dataset has a usable price, everyone receives
    a neutral price_score of 0.5 — the price signal is simply absent.
    Previously the code substituted a 20.0 USD fallback which made all
    normalised prices collapse to 1.0 (identical), silently erasing any
    differentiation. Neutral-0.5 is honest about the missing data.
"""

from dataclasses import dataclass
from typing import Optional

from modules.risk_scorer import ScoredSupplier
from config import (
    RISK_WEIGHT,
    HIGH_RISK_DECAY_START,
    HIGH_RISK_DECAY_END,
    HIGH_RISK_VALUE_CAP,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ValuedSupplier:
    scored:       ScoredSupplier
    price_used:   float    # actual price value used in calculation (USD); 0.0 when unknown
    price_score:  float    # normalised price score (0–1, higher = cheaper = better)
    value_score:  float    # final combined score (0–1, higher = better)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_prices(prices: list[float]) -> list[float]:
    """
    Map a list of prices to [0, 1] where the cheapest gets 1.0
    and the most expensive gets 0.0. Relative to the dataset.
    """
    if not prices:
        return []
    lo, hi = min(prices), max(prices)
    if hi == lo:
        return [1.0] * len(prices)
    return [round(1.0 - (p - lo) / (hi - lo), 4) for p in prices]


def _dataset_fallback_price(known_prices: list[float]) -> Optional[float]:
    """
    Return a fallback price for suppliers with missing price data,
    or None when NO supplier in the dataset has a price at all.

    60th percentile of known prices — slightly above median — so
    "unknown price" suppliers receive a modest penalty rather than
    being treated as the cheapest or most expensive.
    """
    if not known_prices:
        return None
    sorted_p = sorted(known_prices)
    idx = min(int(len(sorted_p) * 0.60), len(sorted_p) - 1)
    return sorted_p[idx]


def _apply_high_risk_decay(value: float, risk_score: float) -> float:
    """
    Linearly blend `value` toward HIGH_RISK_VALUE_CAP as risk_score goes
    from DECAY_START to DECAY_END. Beyond DECAY_END: fully capped.

    Preserves the Risk > Price principle without the rank-flipping
    discontinuity of a hard if/else cap.
    """
    if risk_score <= HIGH_RISK_DECAY_START:
        return value
    if risk_score >= HIGH_RISK_DECAY_END:
        return min(value, HIGH_RISK_VALUE_CAP)

    # Interpolate: 0 at DECAY_START, 1 at DECAY_END
    span   = HIGH_RISK_DECAY_END - HIGH_RISK_DECAY_START
    weight = (risk_score - HIGH_RISK_DECAY_START) / span
    capped = min(value, HIGH_RISK_VALUE_CAP)
    return value * (1.0 - weight) + capped * weight


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_value_scores(scored_suppliers: list[ScoredSupplier]) -> list[ValuedSupplier]:
    """
    Compute value scores for all suppliers.

    Args:
        scored_suppliers: Output of risk_scorer.score_all()

    Returns:
        List of ValuedSupplier, ready for ranking.
    """
    known_prices = [
        s.record.price_est
        for s in scored_suppliers
        if s.record.price_est is not None
    ]
    fallback = _dataset_fallback_price(known_prices)

    if fallback is None:
        # No price data in the entire dataset — give every supplier a neutral
        # price_score of 0.5 instead of collapsing to all-equal 1.0.
        prices     = [0.0] * len(scored_suppliers)
        normalised = [0.5] * len(scored_suppliers)
    else:
        prices = [
            s.record.price_est if s.record.price_est is not None else fallback
            for s in scored_suppliers
        ]
        normalised = _normalise_prices(prices)

    result: list[ValuedSupplier] = []
    for s, p_used, p_norm in zip(scored_suppliers, prices, normalised):

        # ── EXACT FORMULA — unchanged ────────────────────────────────────
        value = p_norm * (1.0 - RISK_WEIGHT * s.risk_score)
        # ─────────────────────────────────────────────────────────────────

        # Soft cap via linear decay — smooth, not a hard step.
        value = round(_apply_high_risk_decay(value, s.risk_score), 4)

        result.append(ValuedSupplier(
            scored=s,
            price_used=p_used,
            price_score=p_norm,
            value_score=value,
        ))

    return result
