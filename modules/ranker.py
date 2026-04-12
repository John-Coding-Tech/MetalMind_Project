"""
modules/ranker.py

Ranking and Top 3 selection module.

Rules (from system/workflow.md, rules/supplier_rules.md):
- Rank all suppliers by value_score descending
- Ensure at least 1 India supplier in Top 3 (India is priority)
- Return exactly Top 3 for recommendation
- Never select based on price alone — value_score already encodes risk
"""

from modules.value_scorer import ValuedSupplier
from modules.risk_scorer  import RiskLevel


def rank_suppliers(
    valued_suppliers: list[ValuedSupplier],
    priority: str = "India",
) -> list[ValuedSupplier]:
    """
    Sort suppliers by value_score descending.

    Args:
        valued_suppliers: Output of compute_value_scores()
        priority: "India" | "Both Equal" | "China"
                  When scores are close, the priority country is favoured.
                  "India"      — guarantee at least 1 India in Top 3
                  "China"      — guarantee at least 1 China in Top 3
                  "Both Equal" — pure value_score order, no country boost
    """
    if not valued_suppliers:
        return []

    # Primary sort: value_score descending
    ranked = sorted(valued_suppliers, key=lambda v: v.value_score, reverse=True)

    if priority == "Both Equal":
        return ranked

    # Determine which country to guarantee in Top 3
    preferred = "India" if priority == "India" else "China"

    top3_countries = {v.scored.record.country for v in ranked[:3]}
    if preferred not in top3_countries:
        best_preferred = next(
            (v for v in ranked[3:] if v.scored.record.country == preferred),
            None,
        )
        if best_preferred:
            ranked.remove(best_preferred)
            ranked.insert(2, best_preferred)

    return ranked


def get_top3(ranked: list[ValuedSupplier]) -> list[ValuedSupplier]:
    """Return the top 3 suppliers from a ranked list."""
    return ranked[:3]


def get_winner(top3: list[ValuedSupplier]) -> ValuedSupplier:
    """Return the #1 recommended supplier from the Top 3."""
    if not top3:
        raise ValueError("Cannot determine winner — no suppliers in list.")
    return top3[0]
