"""
modules/value_scorer.py

Value scoring module.

Formula (from rules/scoring_rules.md):
    Value Score = price_score * (1 - risk_weight)

Where:
    price_score  = normalised inverse price (higher = better value)
    risk_weight  = risk_score from risk_scorer (0 = safe, 1 = dangerous)

A supplier with low price AND low risk scores highest.
A supplier with low price BUT high risk scores low — Risk > Price.
"""

from dataclasses import dataclass

from modules.risk_scorer import ScoredSupplier, RiskLevel


_DEFAULT_PRICE = 20.0   # fallback when no price found — mid-market estimate
_RISK_WEIGHT   = 0.6    # how heavily risk penalises the final score (0–1)


@dataclass
class ValuedSupplier:
    scored:       ScoredSupplier
    price_used:   float    # actual price value used in calculation
    price_score:  float    # normalised price score (0–1, higher = cheaper = better)
    value_score:  float    # final combined score (0–1, higher = better)


def _normalise_prices(prices: list[float]) -> list[float]:
    """
    Map a list of prices to [0, 1] where the cheapest gets 1.0
    and the most expensive gets 0.0.
    """
    if not prices:
        return []
    lo, hi = min(prices), max(prices)
    if hi == lo:
        return [1.0] * len(prices)
    return [round(1.0 - (p - lo) / (hi - lo), 4) for p in prices]


def compute_value_scores(scored_suppliers: list[ScoredSupplier]) -> list[ValuedSupplier]:
    """
    Compute value scores for all suppliers.

    Args:
        scored_suppliers: Output of risk_scorer.score_all()

    Returns:
        List of ValuedSupplier, ready for ranking.
    """
    # Use default price for suppliers where none was found
    prices = [
        s.record.price_est if s.record.price_est is not None else _DEFAULT_PRICE
        for s in scored_suppliers
    ]

    normalised = _normalise_prices(prices)

    result: list[ValuedSupplier] = []
    for s, p_used, p_norm in zip(scored_suppliers, prices, normalised):
        value = round(p_norm * (1.0 - _RISK_WEIGHT * s.risk_score), 4)
        result.append(ValuedSupplier(
            scored=s,
            price_used=p_used,
            price_score=p_norm,
            value_score=value,
        ))

    return result
