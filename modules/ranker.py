"""
modules/ranker.py

Ranking and Top 3 selection module.

Rules (from system/workflow.md, rules/supplier_rules.md):
- Rank all suppliers by value_score descending
- Return exactly Top 3 for recommendation
- Never select based on price alone — value_score already encodes risk
- No country preference: Top 3 is strictly the highest value_scores. The
  "Risk > Price" principle is already encoded in value_score, so forcing
  a particular country into Top 3 would mean promoting a lower-value
  supplier, which contradicts that principle.
"""

from modules.value_scorer import ValuedSupplier


def rank_suppliers(valued_suppliers: list[ValuedSupplier]) -> list[ValuedSupplier]:
    """Sort suppliers by value_score descending."""
    if not valued_suppliers:
        return []
    return sorted(valued_suppliers, key=lambda v: v.value_score, reverse=True)


def get_top3(ranked: list[ValuedSupplier]) -> list[ValuedSupplier]:
    """Return the top 3 suppliers from a ranked list."""
    return ranked[:3]


def get_winner(top3: list[ValuedSupplier]) -> ValuedSupplier:
    """Return the #1 recommended supplier from the Top 3."""
    if not top3:
        raise ValueError("Cannot determine winner — no suppliers in list.")
    return top3[0]
