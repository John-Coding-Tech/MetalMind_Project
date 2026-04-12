"""
engine/recommendation.py

Explanation generator.

Responsibilities (from system/architecture.md Step 8):
- Take the winner and Top 3 as structured data
- Produce a human-readable recommendation with full reasoning
- Explain WHY this supplier was chosen (risk + value logic)
- Never recommend based on price alone — always include risk context

Output (from rules/product_rule.md, prompts/agent_prompt.md):
    - Recommended supplier name
    - Estimated price
    - Risk level
    - Explanation (WHY)
"""

from dataclasses import dataclass

from modules.value_scorer import ValuedSupplier
from modules.risk_scorer  import RiskLevel


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass
class RecommendationResult:
    winner:       ValuedSupplier
    top3:         list[ValuedSupplier]
    summary:      str          # one-line recommendation statement
    explanation:  str          # full multi-sentence explanation
    risk_note:    str          # specific risk commentary for the winner


# ---------------------------------------------------------------------------
# Text generation helpers
# ---------------------------------------------------------------------------

_RISK_DESCRIPTIONS = {
    RiskLevel.LOW:    "low-risk supplier with strong online presence and verifiable information",
    RiskLevel.MEDIUM: "medium-risk supplier — some details could not be fully verified",
    RiskLevel.HIGH:   "high-risk supplier — exercise caution and verify before ordering",
}

def _price_display(price: float) -> str:
    return f"${price:.2f}/sqm (estimated)"

def _risk_badge(level: RiskLevel) -> str:
    return {"Low": "LOW RISK", "Medium": "MEDIUM RISK", "High": "HIGH RISK"}[level.value]


def _build_summary(winner: ValuedSupplier) -> str:
    name    = winner.scored.record.name
    country = winner.scored.record.country
    price   = _price_display(winner.price_used)
    risk    = winner.scored.risk_level.value
    return (
        f"Recommended: {name} ({country}) — "
        f"Est. {price} — Risk: {risk}"
    )


def _build_explanation(
    winner: ValuedSupplier,
    top3: list[ValuedSupplier],
) -> str:
    w  = winner
    w2 = top3[1] if len(top3) > 1 else None
    w3 = top3[2] if len(top3) > 2 else None

    name    = w.scored.record.name
    country = w.scored.record.country
    price   = _price_display(w.price_used)
    risk    = w.scored.risk_level
    v_score = round(w.value_score * 100, 1)

    lines = [
        f"**{name}** is the top-ranked supplier with a value score of {v_score}/100.",
        "",
        f"It is based in **{country}** and is estimated at {price}. "
        f"The supplier is classified as a {_RISK_DESCRIPTIONS[risk]}.",
    ]

    # Why not runner-up?
    if w2:
        n2 = w2.scored.record.name
        r2 = w2.scored.risk_level
        v2 = round(w2.value_score * 100, 1)
        if r2.value != risk.value:
            lines.append(
                f"The runner-up, **{n2}**, has a higher risk level ({r2.value}) "
                f"which reduced its value score to {v2}/100 — below {name}."
            )
        else:
            lines.append(
                f"The runner-up, **{n2}**, scores {v2}/100 — close, "
                f"but {name} offered better overall value after risk adjustment."
            )

    # India priority note
    if country == "India":
        lines.append(
            "India is the preferred sourcing region per supplier rules, "
            "offering shorter lead times and potentially lower freight costs."
        )
    elif country == "China":
        lines.append(
            "Note: Although India is the preferred region, no Indian supplier "
            "scored higher on the combined value + risk metric for this search."
        )

    # Price-only warning
    lines.append(
        "\n_This recommendation is based on risk-adjusted value, not lowest price alone. "
        "Always confirm current pricing and credentials directly with the supplier._"
    )

    return "\n".join(lines)


def _build_risk_note(winner: ValuedSupplier) -> str:
    level   = winner.scored.risk_level
    reasons = winner.scored.risk_reasons
    badge   = _risk_badge(level)

    note = f"[{badge}] "
    if level == RiskLevel.LOW:
        note += "Supplier shows strong credibility signals."
    elif level == RiskLevel.MEDIUM:
        note += "Some caution advised — verify the following before ordering:"
    else:
        note += "High caution — do NOT proceed without verifying:"

    if reasons and reasons != ["No risk signals detected"]:
        items = "\n  • ".join(reasons)
        note += f"\n  • {items}"

    return note


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_recommendation(
    winner: ValuedSupplier,
    top3: list[ValuedSupplier],
) -> RecommendationResult:
    """
    Generate a full recommendation result from the winner and Top 3.

    Args:
        winner: The #1 ranked ValuedSupplier
        top3:   Top 3 ValuedSupplier list (winner must be top3[0])

    Returns:
        RecommendationResult with summary, explanation, and risk note
    """
    return RecommendationResult(
        winner=winner,
        top3=top3,
        summary=_build_summary(winner),
        explanation=_build_explanation(winner, top3),
        risk_note=_build_risk_note(winner),
    )
